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
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

Status = Literal["current", "superseded", "contaminated", "unknown"]

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
    """Per-dataset contamination outcome."""

    name: str
    version: str
    dataset_hash: str
    split_hash: str
    status: Status


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


def read_facets(capsule_dir: str | Path) -> list[DatasetProvenanceFacet]:
    """Read all dataset-provenance facets from a capsule (empty when none present)."""
    capsule_dir = Path(capsule_dir)
    facets: list[DatasetProvenanceFacet] = []
    facet_dir = capsule_dir / FACET_DIRNAME
    if facet_dir.is_dir():
        for path in sorted(facet_dir.glob("*.json")):
            try:
                facets.append(DatasetProvenanceFacet.model_validate_json(path.read_text()))
            except Exception:  # noqa: BLE001 - a malformed facet is skipped, not fatal
                continue
    return facets


def check_contamination(
    capsule_dir: str | Path, registry: ContaminationRegistry | None
) -> list[ContaminationResult]:
    """Resolve each capsule dataset facet's status against *registry* (NF-028 R8).

    With no registry, the resolved status is each facet's own recorded ``status``
    (so a facet pre-flagged ``contaminated`` still surfaces). With a registry, the
    registry decision wins when it is more severe than the recorded status.
    """
    results: list[ContaminationResult] = []
    for facet in read_facets(capsule_dir):
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
    return results


def is_flagged(results: list[ContaminationResult]) -> bool:
    """True when any dataset is contaminated or superseded (drives exit code 4)."""
    return any(r.status in ("contaminated", "superseded") for r in results)


def as_dicts(results: list[ContaminationResult]) -> list[dict[str, Any]]:
    return [r.model_dump() for r in results]
