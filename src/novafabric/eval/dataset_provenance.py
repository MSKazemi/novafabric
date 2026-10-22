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

"""Dataset-provenance facet + benchmark-contamination check (NF-028, ADR-0108).

Records the **dataset + split content hashes** an eval ran against so a capsule can be
flagged when it was run on a *contaminated* or *superseded* benchmark version
(contamination silently inflates scores; a provenance fabric should surface it). This is
detection/flagging only — no remediation.

The facet is stored additively under the capsule's
``extensions/dev.novafabric.dataset-provenance/`` namespace (run-capsule-v1 §extensions);
absence of the facet leaves today's behavior unchanged. ``check_contamination`` compares
each facet's hashes against a **configurable** known-contaminated/superseded registry
(no hardcoded URL) and resolves a status per dataset.

The check **fails closed**: a facet file that cannot be read, parsed or validated —
or a facet namespace that is not a directory — is reported as an ``invalid`` verdict
with a reason and is flagged exactly like contamination. Skipping it would let a
capsule whose only provenance record is unreadable pass a CI contamination gate.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

_log = logging.getLogger(__name__)

Status = Literal["current", "superseded", "contaminated", "unknown"]

#: A check verdict: a facet ``Status``, or ``invalid`` for a facet the check could
#: not read or validate. ``invalid`` is never a recordable facet status.
Verdict = Literal["current", "superseded", "contaminated", "unknown", "invalid"]

#: Verdicts that make the check non-OK (exit 4).
FLAGGED_VERDICTS: frozenset[str] = frozenset({"contaminated", "superseded", "invalid"})

#: additive capsule namespace for the facet (run-capsule-v1 §extensions).
FACET_DIRNAME = Path("extensions") / "dev.novafabric.dataset-provenance"

# contaminated is the most severe; unknown the least.
_SEVERITY: dict[str, int] = {"contaminated": 3, "superseded": 2, "current": 1, "unknown": 0}


class DatasetProvenanceFacet(BaseModel):
    """A ``dataset_provenance`` facet (NF-028 R6)."""

    name: str = Field(min_length=1)
    version: str = ""
    dataset_hash: str = ""
    split_hash: str = ""
    status: Status = "unknown"


class ContaminationRegistry(BaseModel):
    """A configurable known-contaminated/superseded hash registry (NF-028 R8).

    ``contaminated`` / ``superseded`` are lists of ``sha256:`` hashes; a facet whose
    ``dataset_hash`` **or** ``split_hash`` appears in a list resolves to that status.
    """

    contaminated: list[str] = Field(default_factory=list)
    superseded: list[str] = Field(default_factory=list)

    @classmethod
    def from_path(cls, path: Path) -> ContaminationRegistry:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)

    def resolve(self, facet: DatasetProvenanceFacet) -> Status:
        hashes = {h for h in (facet.dataset_hash, facet.split_hash) if h}
        if hashes & set(self.contaminated):
            return "contaminated"
        if hashes & set(self.superseded):
            return "superseded"
        if hashes:
            return "current"
        return "unknown"


class ContaminationResult(BaseModel):
    """Per-dataset contamination outcome.

    ``reason`` is set only for an ``invalid`` verdict and says why the facet
    could not be checked.
    """

    name: str
    version: str
    dataset_hash: str
    split_hash: str
    status: Verdict
    reason: str | None = None


class InvalidFacet(BaseModel):
    """A facet file (or facet namespace) that could not be read or validated."""

    path: Path
    reason: str


def write_facet(capsule_dir: str | Path, facet: DatasetProvenanceFacet) -> Path:
    """Write a facet into the capsule's dataset-provenance extension namespace (additive)."""
    capsule_dir = Path(capsule_dir)
    facet_dir = capsule_dir / FACET_DIRNAME
    facet_dir.mkdir(parents=True, exist_ok=True)
    # deterministic filename from the dataset name (slugified).
    slug = "".join(c if c.isalnum() else "-" for c in facet.name.lower()).strip("-") or "dataset"
    out = facet_dir / f"{slug}.json"
    out.write_text(facet.model_dump_json(indent=2), encoding="utf-8")
    return out


def _load_facet(path: Path) -> DatasetProvenanceFacet | InvalidFacet:
    """Parse one facet file; never raises — a failure becomes an ``InvalidFacet``."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return InvalidFacet(path=path, reason=f"could not read {path.name}: {exc.strerror or exc}")
    try:
        return DatasetProvenanceFacet.model_validate_json(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        return InvalidFacet(path=path, reason=f"{path.name} is not a valid facet: {problems}")


def scan_facets(
    capsule_dir: str | Path,
) -> tuple[list[DatasetProvenanceFacet], list[InvalidFacet]]:
    """Read every facet in a capsule, keeping the ones that failed alongside the valid.

    Returns ``(valid, invalid)``. A facet namespace that exists but is not a
    directory is itself one ``InvalidFacet``. Never raises.
    """
    facet_dir = Path(capsule_dir) / FACET_DIRNAME
    valid: list[DatasetProvenanceFacet] = []
    invalid: list[InvalidFacet] = []
    if not facet_dir.exists():
        return valid, invalid
    if not facet_dir.is_dir():
        reason = f"facet namespace {FACET_DIRNAME} is not a directory"
        invalid.append(InvalidFacet(path=facet_dir, reason=reason))
        return valid, invalid
    for path in sorted(facet_dir.glob("*.json")):
        loaded = _load_facet(path)
        if isinstance(loaded, InvalidFacet):
            _log.warning("dataset-provenance facet rejected: %s", loaded.reason)
            invalid.append(loaded)
        else:
            valid.append(loaded)
    return valid, invalid


def read_facets(capsule_dir: str | Path) -> list[DatasetProvenanceFacet]:
    """Read the *valid* dataset-provenance facets of a capsule (empty when none).

    Malformed facets are left out here. A caller that must not treat "unreadable"
    as "absent" — any gate — uses :func:`scan_facets` or :func:`check_contamination`,
    which report them.
    """
    return scan_facets(capsule_dir)[0]


def check_contamination(
    capsule_dir: str | Path, registry: ContaminationRegistry | None
) -> list[ContaminationResult]:
    """Resolve each capsule dataset facet's status against *registry* (NF-028 R8).

    With no registry, the resolved status is each facet's own recorded ``status``
    (so a facet pre-flagged ``contaminated`` still surfaces). With a registry, the
    registry decision wins when it is more severe than the recorded status.

    Fails closed: each facet that could not be read or validated yields an
    ``invalid`` result with a ``reason``; no registry can clear it.
    """
    valid, invalid = scan_facets(capsule_dir)
    results: list[ContaminationResult] = []
    for facet in valid:
        resolved: Status = facet.status
        if registry is not None:
            reg_status = registry.resolve(facet)
            if _SEVERITY[reg_status] > _SEVERITY[resolved]:
                resolved = reg_status
        results.append(
            ContaminationResult(
                name=facet.name,
                version=facet.version,
                dataset_hash=facet.dataset_hash,
                split_hash=facet.split_hash,
                status=resolved,
            )
        )
    for bad in invalid:
        results.append(
            ContaminationResult(
                name=bad.path.stem,
                version="",
                dataset_hash="",
                split_hash="",
                status="invalid",
                reason=bad.reason,
            )
        )
    return results


def is_flagged(results: list[ContaminationResult]) -> bool:
    """True when any dataset is contaminated, superseded or invalid (drives exit code 4)."""
    return any(r.status in FLAGGED_VERDICTS for r in results)


def as_dicts(results: list[ContaminationResult]) -> list[dict[str, Any]]:
    return [r.model_dump() for r in results]
