# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Dataset-experiment record — the ADR-0120 ``Experiment`` model and file store.

An ``Experiment`` is an **immutable, content-addressed** record of one target run
across every item of a *pinned* dataset: it links each dataset item to the Run
Capsule and the evidence-grade :class:`~novafabric.eval.scores.Score` records
(ADR-0099) it produced, and carries a per-metric aggregate. Wire contract:
``schemas/experiment.schema.json`` (graduated from ``design/spec/schemas/``);
companion spec ``design/spec/dataset-experiment-v0.md``.

Invariants (ADR-0120 D1):

- **Immutability** — a finalized record is never mutated; a re-run is a *new*
  experiment with a new ``experiment_id``. The store refuses to overwrite, and a
  stored record whose ``content_hash`` does not match its recomputed canonical
  body is rejected at parse time (tamper-evident, mirrors ADR-0117 C5).
- **Additive** — this is a new optional artifact; the Run Capsule schema, the
  ``Score`` schema, and existing eval outputs are unchanged.

The per-item runner lives in :mod:`novafabric.eval.experiment_runner`; the A/B
comparison in :mod:`novafabric.eval.experiment_compare`. This module is pure data
plus a small JSON file store — stdlib hashing only, no new dependency.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from novafabric.capture._ulid import new_ulid

EXPERIMENT_SCHEMA_VERSION: Literal["0.1.0"] = "0.1.0"

_ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExperimentError(Exception):
    """Base class for dataset-experiment errors."""


class ExperimentNotFoundError(ExperimentError):
    """No stored experiment matches the given reference."""


class ExperimentExistsError(ExperimentError):
    """A stored experiment with this id already exists (immutability, D1)."""


class ExperimentFinalizedError(ExperimentError):
    """A finalized experiment record was asked to change (immutability, D1)."""


class TargetKind(str, Enum):
    """What kind of asset the experiment ran (ADR-0112/0113 target)."""

    ASSET = "asset"
    PROMPT = "prompt"
    AGENT = "agent"


class ItemRunStatus(str, Enum):
    """Outcome of one dataset item's run."""

    OK = "ok"
    ERROR = "error"
    SKIPPED = "skipped"


class DatasetRef(BaseModel):
    """Pinned dataset identity — mirrors ``DatasetProvenanceFacet`` (ADR-0108)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    dataset_hash: str
    split_hash: str

    @model_validator(mode="after")
    def _check(self) -> DatasetRef:
        for field in ("dataset_hash", "split_hash"):
            value = getattr(self, field)
            if not _SHA256_RE.match(value):
                raise ValueError(f"{field} must be a 'sha256:<64hex>' digest: {value!r}")
        return self


class ExperimentTarget(BaseModel):
    """The asset/prompt version under test — the *resolved* ref, never an alias."""

    model_config = ConfigDict(extra="forbid")

    kind: TargetKind
    ref: str = Field(min_length=1)
    label: str | None = None


class ItemRun(BaseModel):
    """One dataset item's run: dataset item → Run Capsule → score records."""

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)
    capsule_ref: str = Field(min_length=1)
    score_ids: list[str] = Field(default_factory=list)
    status: ItemRunStatus = ItemRunStatus.OK

    @model_validator(mode="after")
    def _check(self) -> ItemRun:
        for score_id in self.score_ids:
            if not _ULID_RE.match(score_id):
                raise ValueError(f"score_ids entry is not a valid ULID: {score_id!r}")
        return self


class MetricAggregate(BaseModel):
    """Per-metric aggregate over an experiment's item runs."""

    model_config = ConfigDict(extra="forbid")

    metric: str = Field(min_length=1)
    value_type: Literal["boolean", "numeric"]
    reducer: str = Field(min_length=1)
    value: float
    n: int = Field(ge=0)
    wilson: tuple[float, float] | None = None


class Experiment(BaseModel):
    """An immutable dataset-experiment record (ADR-0120 D1).

    ``content_hash`` may be omitted while ``status`` is ``running``; a finalized
    record MUST carry ``finalized_at`` and a ``content_hash`` that equals the
    recomputed digest of its canonical body — a tampered record fails to parse.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = EXPERIMENT_SCHEMA_VERSION
    experiment_id: str = Field(default_factory=new_ulid)
    dataset_ref: DatasetRef
    target: ExperimentTarget
    runs: list[ItemRun]
    aggregate: list[MetricAggregate]
    status: Literal["running", "finalized"]
    created_at: str = Field(default_factory=_now_iso)
    score_config_ref: str | None = None
    finalized_at: str | None = None
    content_hash: str | None = None
    baseline_experiment_id: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    extensions: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check(self) -> Experiment:
        if not _ULID_RE.match(self.experiment_id):
            raise ValueError(f"experiment_id is not a valid ULID: {self.experiment_id!r}")
        if self.baseline_experiment_id is not None and not _ULID_RE.match(
            self.baseline_experiment_id
        ):
            raise ValueError(
                f"baseline_experiment_id is not a valid ULID: {self.baseline_experiment_id!r}"
            )
        if self.status == "running":
            if self.content_hash is not None or self.finalized_at is not None:
                raise ValueError(
                    "a running experiment must not carry content_hash or finalized_at"
                )
        else:  # finalized
            if self.finalized_at is None:
                raise ValueError("a finalized experiment requires finalized_at")
            if self.content_hash is None:
                raise ValueError("a finalized experiment requires content_hash")
            derived = experiment_content_hash(self)
            if self.content_hash != derived:
                raise ValueError(
                    f"content_hash does not match the canonical body: "
                    f"got {self.content_hash!r}, derived {derived!r}"
                )
        return self


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Canonical-JSON bytes of a record body with ``content_hash`` excluded.

    Deterministic: keys sorted, no insignificant whitespace, unset/null optionals
    dropped, enums already rendered as their string values (ADR-0120 D1).
    """
    body = {k: v for k, v in payload.items() if k != "content_hash" and v is not None}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_experiment_bytes(experiment: Experiment) -> bytes:
    """Canonical-JSON bytes of *experiment* with ``content_hash`` excluded."""
    return _canonical_bytes(experiment.model_dump(mode="json", exclude_none=True))


def experiment_content_hash(experiment: Experiment) -> str:
    """``sha256:<hex>`` digest over the record's canonical body (D1)."""
    return "sha256:" + hashlib.sha256(canonical_experiment_bytes(experiment)).hexdigest()


def finalize_experiment(experiment: Experiment) -> Experiment:
    """Return a **new** finalized, content-hashed copy of a running *experiment*.

    Raises :class:`ExperimentFinalizedError` if the record is already finalized —
    a finalized experiment is never re-finalized or mutated (D1).
    """
    if experiment.status == "finalized":
        raise ExperimentFinalizedError(
            f"experiment {experiment.experiment_id} is already finalized"
        )
    body = experiment.model_dump(mode="json", exclude_none=True)
    body["status"] = "finalized"
    body["finalized_at"] = _now_iso()
    body["content_hash"] = "sha256:" + hashlib.sha256(_canonical_bytes(body)).hexdigest()
    return Experiment.model_validate(body)


# ── File store (local JSON; one file per experiment) ─────────────────────────


def default_experiments_dir() -> Path:
    """Project-level experiments directory: ``$PWD/.novafabric/experiments``.

    Override with ``NOVAFABRIC_EXPERIMENTS_DIR``. Follows the project-config
    convention (``.novafabric/`` in the working directory, ADR-0029/0130);
    created lazily on first save.
    """
    env = os.environ.get("NOVAFABRIC_EXPERIMENTS_DIR")
    return Path(env) if env else Path.cwd() / ".novafabric" / "experiments"


def save_experiment(experiment: Experiment, experiments_dir: Path | None = None) -> Path:
    """Persist *experiment* as ``<experiment_id>.json``; never overwrites (D1)."""
    directory = experiments_dir if experiments_dir is not None else default_experiments_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{experiment.experiment_id}.json"
    if path.exists():
        raise ExperimentExistsError(
            f"experiment {experiment.experiment_id} already exists at {path}; "
            "a re-run must mint a new experiment_id (ADR-0120 D1)"
        )
    payload = experiment.model_dump(mode="json", exclude_none=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _parse_experiment_file(path: Path) -> Experiment:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ExperimentError(f"cannot read experiment file {path}: {exc}") from exc
    try:
        return Experiment.model_validate_json(text)
    except ValueError as exc:
        raise ExperimentError(f"invalid experiment record {path}: {exc}") from exc


def load_experiment(ref: str, experiments_dir: Path | None = None) -> Experiment:
    """Load one experiment by id (from the store) or by explicit file path."""
    as_path = Path(ref)
    if as_path.is_file():
        return _parse_experiment_file(as_path)
    directory = experiments_dir if experiments_dir is not None else default_experiments_dir()
    candidate = directory / f"{ref}.json"
    if candidate.is_file():
        return _parse_experiment_file(candidate)
    raise ExperimentNotFoundError(f"no experiment {ref!r} in {directory}")


def list_experiments(experiments_dir: Path | None = None) -> list[Experiment]:
    """All stored experiments, oldest first (empty when the store is absent)."""
    directory = experiments_dir if experiments_dir is not None else default_experiments_dir()
    if not directory.is_dir():
        return []
    records = [_parse_experiment_file(p) for p in sorted(directory.glob("*.json"))]
    records.sort(key=lambda e: e.created_at)
    return records
