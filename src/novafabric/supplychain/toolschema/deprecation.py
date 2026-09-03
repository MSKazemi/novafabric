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

"""Tool-deprecation lineage (ADR-0148 D2 / NF-169).

Records a retired tool version and flags every sealed run still pinned to it, so a deprecation
announcement can be turned into the list of runs it actually affects.

**Where the dependent runs come from, and why it is not lineage.** The spec asks this to
*"reference the lineage edges of dependent runs"*. The lineage graph has no tool nodes to
reference: ``lineage/_writer.py`` emits ``run``, ``asset`` and ``artifact`` and nothing else, so a
lineage-based implementation would return an empty list for every input and read as *"no run is
affected"*. The data does exist — ``tool_version`` is a **required** field of every
``tool-calls.jsonl`` record — so the dependent set is derived from the sealed capsules, the same
source NF-165 already uses for historical payloads. Emitting tool nodes into lineage is the
follow-on, and it is a capture-path change.

**⚠ A version of ``unknown`` is a third answer, not a no.** ``tool_version``'s own schema says
*"Semver if known; ``unknown`` otherwise"*. A run recorded that way can be neither confirmed as
pinned to the retired version nor cleared of it. Folding it into the dependent list over-reports;
dropping it silently under-reports while looking complete. It gets its own bucket.

**``capsules_scanned`` travels with the answer** for the same reason: an empty dependent list
drawn from three capsules is not the claim an empty list drawn from three thousand makes, and
without the count "nothing was affected" and "nothing was searched" serialise identically.

It **reports; it does not gate** — there is no verdict field, and a pinned run is a fact to act
on, not a decision this module makes.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from novafabric.drift.collect import read_trajectory
from novafabric.query.indexer import scan_capsule

SCHEMA_VERSION = "0.1.0"
FACET_NAME = "tool_deprecation"

#: The value ``tool_version`` carries when the capture path could not determine a version
#: (``schemas/tool-call.schema.json``). Never treated as a match, never treated as a clearance.
UNKNOWN_VERSION = "unknown"


class ToolDeprecationError(ValueError):
    """Raised when a deprecation record cannot be built honestly."""


class ToolDeprecation(BaseModel):
    """A retired tool version and the sealed runs still pinned to it.

    Intentionally carries no verdict field: "these runs use a retired version" is a fact, and
    what to do about it is not this module's call.
    """

    model_config = ConfigDict(frozen=True)

    tool_id: str
    deprecated_version: str
    deprecated_at: str
    #: Absent when no successor was declared — never ``""`` or ``"none"``, which would read as a
    #: successor that exists and is unnamed.
    successor: str | None = None
    dependent_run_ids: list[str] = Field(default_factory=list)
    #: Runs that called this tool at version ``unknown``: neither confirmed nor cleared.
    unknown_version_run_ids: list[str] = Field(default_factory=list)
    #: How many capsules were examined. Without it, "nothing affected" and "nothing searched"
    #: cannot be told apart.
    capsules_scanned: int = 0
    schema_version: str = SCHEMA_VERSION

    @field_validator("deprecated_at")
    @classmethod
    def _valid_timestamp(cls, value: str) -> str:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"deprecated_at {value!r} is not an ISO-8601 timestamp; a deprecation date that "
                "cannot be parsed cannot be compared to anything"
            ) from exc
        return value

    @field_validator("successor")
    @classmethod
    def _successor_is_named_or_absent(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError(
                "successor must name a tool or be omitted; an empty string reads as a successor "
                "that exists and was not named"
            )
        return value


def scan_for_dependents(
    capsule_dir: str | Path, *, tool_id: str, version: str
) -> tuple[list[str], list[str], int]:
    """Find sealed runs that called *tool_id*, split by whether they pin *version*.

    Returns ``(dependent_run_ids, unknown_version_run_ids, capsules_scanned)``. A run appears in
    the first list only when its recorded version *equals* the retired one, and in the second when
    the capture path recorded :data:`UNKNOWN_VERSION` — a run that cannot be judged either way.

    Uses the shared strict tool-call reader
    (:func:`novafabric.drift.collect.read_trajectory`) rather than a third parser of the same
    file, and the shared definition of what a capsule is
    (:func:`novafabric.query.indexer.scan_capsule`).

    Raises:
        ToolDeprecationError: if *capsule_dir* is not a directory.
    """
    base = Path(capsule_dir)
    if not base.is_dir():
        raise ToolDeprecationError(f"capsule directory not found: {base}")

    dependent: list[str] = []
    unknown: list[str] = []
    scanned = 0
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        scanned_rows = scan_capsule(child)
        if scanned_rows is None:
            continue
        scanned += 1
        # The manifest's run_id wins over the directory name — taken from the scanner's own rows
        # rather than re-derived, so a capsule whose directory was renamed is still reported
        # under the id it recorded.
        calls_rows, _ = scanned_rows
        run_id = calls_rows[0].run_id if calls_rows else child.name
        calls = read_trajectory(child)
        versions = {c["version"] for c in calls if c["name"] == tool_id}
        if version in versions:
            dependent.append(run_id)
        elif UNKNOWN_VERSION in versions:
            unknown.append(run_id)
    return dependent, unknown, scanned


def build_deprecation(
    capsule_dir: str | Path,
    *,
    tool_id: str,
    deprecated_version: str,
    deprecated_at: str,
    successor: str | None = None,
) -> ToolDeprecation:
    """Record a retired tool version together with the runs still pinned to it.

    Raises:
        ToolDeprecationError: if the capsule directory is missing, or the record is not
            internally honest (an unparseable ``deprecated_at``, an empty ``successor``).
    """
    if deprecated_version == UNKNOWN_VERSION:
        raise ToolDeprecationError(
            f"cannot deprecate version {UNKNOWN_VERSION!r}: it is the value recorded when the "
            "capture path could not determine a version, so every run carrying it would be "
            "flagged as pinned to a version nobody released"
        )
    dependent, unknown, scanned = scan_for_dependents(
        capsule_dir, tool_id=tool_id, version=deprecated_version
    )
    try:
        return ToolDeprecation(
            tool_id=tool_id,
            deprecated_version=deprecated_version,
            deprecated_at=deprecated_at,
            successor=successor,
            dependent_run_ids=dependent,
            unknown_version_run_ids=unknown,
            capsules_scanned=scanned,
        )
    except ValueError as exc:
        raise ToolDeprecationError(str(exc)) from exc


# ── Capsule facet ─────────────────────────────────────────────────────────


def attach_facet(
    capsule: dict[str, Any], deprecation: ToolDeprecation | None
) -> dict[str, Any]:
    """Attach *deprecation* to a capsule dict additively; returns a new dict."""
    if deprecation is None:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    facets[FACET_NAME] = deprecation.model_dump(exclude_none=True)
    out["facets"] = facets
    return out


def facet_from_capsule(capsule: dict[str, Any]) -> ToolDeprecation | None:
    """Read a tool-deprecation record back out of a capsule dict, or None if absent."""
    facets = capsule.get("facets")
    if not isinstance(facets, dict):
        return None
    block = facets.get(FACET_NAME)
    if not isinstance(block, dict):
        return None
    try:
        return ToolDeprecation.model_validate(block)
    except ValueError as exc:
        raise ToolDeprecationError(
            f"capsule holds an invalid {FACET_NAME} facet: {exc}"
        ) from exc


__all__ = [
    "FACET_NAME",
    "SCHEMA_VERSION",
    "UNKNOWN_VERSION",
    "ToolDeprecation",
    "ToolDeprecationError",
    "attach_facet",
    "build_deprecation",
    "facet_from_capsule",
    "scan_for_dependents",
]
