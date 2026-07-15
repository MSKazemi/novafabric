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

"""Unified evidence-grade ``Score`` record (NF-010, ADR-0099).

The evidence-grade reframe: *a score is not a number — it is a signed, replayable
record binding ``(value, evaluator-identity, subject-span-digest, verdict)`` into a
Run Capsule*. This module defines the additive, optional ``scores.jsonl`` capsule
file and its record model.

Design invariants (see ``design/spec/features/NF-002-010-signed-eval-cards.md``):

- **Additive-first (SPK-EVAL-1):** ``scores.jsonl`` is optional; a capsule without it
  stays valid. This module never mutates an existing capsule file other than the
  append-only score log.
- **Content-addressed:** ``subject`` and ``eval_card_digest`` are ``sha256:`` hashes,
  not names — a score is reproducible to the exact evaluator (eval card) and subject.
- **Zero-token by construction for non-judge scorers:** this module performs no model
  calls; ``heuristic``/``code`` scores are pure data.

The signed *eval card* that a score references lives in ``eval/card.py`` (NF-002).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator

from pydantic import BaseModel, ConfigDict, Field, model_validator

from novafabric.capture._ulid import new_ulid

SCORE_SCHEMA_VERSION = "1.0.0"

#: Canonical name of the optional per-capsule score log.
SCORES_FILENAME = "scores.jsonl"

# ── Shared identity patterns (reuse capsule/envelope conventions) ─────────────
_ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ScoreValueType(str, Enum):
    """The type of a score value (requirement 1)."""

    BOOLEAN = "boolean"
    CATEGORICAL = "categorical"
    NUMERIC = "numeric"


class ScoreSource(str, Enum):
    """Provenance of a score (requirement 1, NF-010).

    ``heuristic`` and ``code`` scorers run offline on stored capsules at zero token
    cost; only ``judge`` scorers may spend tokens, and only on sampled subjects.
    """

    HUMAN = "human"
    HEURISTIC = "heuristic"
    CODE = "code"
    JUDGE = "judge"


class SignificanceBlock(BaseModel):
    """Optional ADR-0080 significance context carried on a score (requirement 7)."""

    model_config = ConfigDict(extra="forbid")

    method: str = Field(pattern=r"^(wilson|sprt)$")
    ci_low: float
    ci_high: float
    confidence: float = Field(ge=0.0, le=1.0)


class Score(BaseModel):
    """A single evidence-grade evaluation score (``scores.jsonl`` line).

    See ``schemas/score-v1.schema.json`` for the wire contract. Validation enforces
    the requirement-1/-2 invariants: ULID ids, ``sha256:`` content-addressed subject
    and eval-card digest, and value/value_type agreement.
    """

    model_config = ConfigDict(extra="forbid")

    score_id: str = Field(default_factory=new_ulid)
    subject: str
    subject_kind: str = "span"
    name: str = Field(min_length=1)
    # Order matters for pydantic smart-union: bool before float so JSON ``true``
    # does not coerce to ``1.0``; the after-validator is the authority regardless.
    value: bool | float | str
    value_type: ScoreValueType
    source: ScoreSource
    evaluator_id: str = Field(min_length=1)
    eval_card_digest: str
    significance: SignificanceBlock | None = None
    created_at: str = Field(default_factory=_now_iso)
    run_id: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Score:
        if not _ULID_RE.match(self.score_id):
            raise ValueError(f"score_id is not a valid ULID: {self.score_id!r}")
        if self.subject_kind not in ("span", "capsule"):
            raise ValueError(
                f"subject_kind must be 'span' or 'capsule', got {self.subject_kind!r}"
            )
        if not _SHA256_RE.match(self.subject):
            raise ValueError(f"subject must be a 'sha256:<hex>' digest: {self.subject!r}")
        if not _SHA256_RE.match(self.eval_card_digest):
            raise ValueError(
                f"eval_card_digest must be a 'sha256:<hex>' digest: {self.eval_card_digest!r}"
            )
        if self.run_id is not None and not _ULID_RE.match(self.run_id):
            raise ValueError(f"run_id is not a valid ULID: {self.run_id!r}")
        # value/value_type agreement (requirement 1). bool is a subclass of int but
        # NOT of float, so these isinstance checks are mutually exclusive.
        if self.value_type is ScoreValueType.BOOLEAN and not isinstance(self.value, bool):
            raise ValueError("value_type 'boolean' requires a bool value")
        if self.value_type is ScoreValueType.CATEGORICAL and not isinstance(self.value, str):
            raise ValueError("value_type 'categorical' requires a string value")
        if self.value_type is ScoreValueType.NUMERIC and (
            isinstance(self.value, bool) or not isinstance(self.value, (int, float))
        ):
            raise ValueError("value_type 'numeric' requires a numeric (non-bool) value")
        return self


def read_scores(path: str | Path) -> list[Score]:
    """Read and validate every ``Score`` from a ``scores.jsonl`` file.

    Returns an empty list if the file does not exist (additive-first: a capsule
    without a score log is valid, requirement 3). Blank lines are skipped.
    """
    p = Path(path)
    if not p.exists():
        return []
    scores: list[Score] = []
    for line_no, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            scores.append(Score.model_validate_json(line))
        except Exception as exc:  # noqa: BLE001 - re-raise with file context
            raise ValueError(f"{p}:{line_no}: invalid Score record: {exc}") from exc
    return scores


def iter_scores(path: str | Path) -> Iterator[Score]:
    """Stream ``Score`` records from a ``scores.jsonl`` file (memory-bounded)."""
    p = Path(path)
    if not p.exists():
        return
    with p.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                yield Score.model_validate_json(line)
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"{p}:{line_no}: invalid Score record: {exc}") from exc


def boolean_metric_outcomes(scores: Iterable[Score], metric: str) -> list[int]:
    """0/1 pass/fail outcomes for a boolean ``metric``, in the given order.

    A bridge for the ADR-0080 significance gate (NF-007): turns evidence-grade
    ``scores.jsonl`` records into the Bernoulli sequence the SPRT consumes. Scores whose
    ``name`` differs from *metric* are skipped; a matching non-boolean score is an error.
    """
    outcomes: list[int] = []
    for score in scores:
        if score.name != metric:
            continue
        if score.value_type is not ScoreValueType.BOOLEAN:
            raise ValueError(
                f"metric {metric!r} score is not boolean (value_type={score.value_type.value})"
            )
        outcomes.append(1 if score.value else 0)
    return outcomes


def append_score(path: str | Path, score: Score) -> None:
    """Append one ``Score`` as a JSONL line, creating the file if needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(score.model_dump_json(exclude_none=True) + "\n")


def write_scores(path: str | Path, scores: Iterable[Score]) -> None:
    """Write (overwrite) a ``scores.jsonl`` file from an iterable of scores."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        for score in scores:
            fh.write(score.model_dump_json(exclude_none=True) + "\n")
