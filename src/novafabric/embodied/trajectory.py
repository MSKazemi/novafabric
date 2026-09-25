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

"""Perception → decision → actuation trajectory chain — ADR-0162 P2 (NF-310).

``facets.embodied.trajectory`` is an ordered list of hops, each
``{stage, input_digest, output_digest, parent, ts}``, where ``parent`` names an
*earlier* hop's ``output_digest``. It is a provenance chain of declared or
observed artifact digests — **not** a control loop. NovaFabric walks it; it
never re-runs perception, re-derives the decision, or actuates (I-3/I-4).

**Recording and verifying are separate on purpose.** A hop is validated for
shape at construction (digests, stage, offset-aware ``ts``, no raw payload),
but a *broken chain* is not refused: a capsule whose decision hop points at a
perception output nobody recorded is exactly the evidence an incident
reconstruction needs to see, and refusing to load it would hide it.
:func:`walk_trajectory` is the verifier; it names every defect by hop index.

The walk's results are **not** stored in the facet. A stored
``no_broken_parent: true`` would be a self-attestation the next reader has to
re-check anyway; the chain itself is the evidence, and the walk is cheap and
offline.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from novafabric.embodied._boundary import _own_fields, _validate_ref, reject_raw_payloads
from novafabric.embodied._timestamps import parse_ts

#: The four hop stages NF-310 enumerates, in provenance order.
TrajectoryStage = Literal["perception", "world_model", "decision", "actuation"]

_STAGE_RANK: dict[str, int] = {
    "perception": 0,
    "world_model": 1,
    "decision": 2,
    "actuation": 3,
}

FindingCode = Literal[
    "empty_chain",
    "missing_parent",
    "broken_parent",
    "forward_reference",
    "self_reference",
    "duplicate_output",
    "ts_regression",
    "stage_regression",
    "chain_too_long",
]

#: Upper bound on the walk; far beyond any real recorded chain, and it keeps a
#: hostile capsule from turning verification into an unbounded job. Mirrors
#: :data:`novafabric.preservation.format_migration.MAX_CHAIN_LENGTH`.
MAX_HOPS = 10_000

#: Codes that mean the chain cannot be walked as a DAG.
_CYCLE_CODES = frozenset({"self_reference", "forward_reference"})
#: Codes that mean a hop's provenance does not resolve to an earlier hop.
_BROKEN_CODES = frozenset({"missing_parent", "broken_parent"})


class TrajectoryHop(BaseModel):
    """One declared/observed hop in the perception→actuation chain (NF-310)."""

    model_config = ConfigDict(extra="allow")

    stage: TrajectoryStage
    #: ``sha256:`` of what the stage consumed.
    input_digest: str
    #: ``sha256:`` of what the stage produced; later hops cite it as ``parent``.
    output_digest: str
    #: An earlier hop's ``output_digest``; ``None`` only for the chain root.
    parent: str | None = None
    #: Offset-aware ISO-8601 instant, stored as declared.
    ts: str

    @field_validator("input_digest", "output_digest", "parent", mode="before")
    @classmethod
    def _check_refs(cls, value: Any) -> Any:
        if value is None or isinstance(value, str):
            return _validate_ref(value)
        reject_raw_payloads(value, path="digest")
        return value

    @field_validator("ts", mode="before")
    @classmethod
    def _check_ts(cls, value: Any) -> Any:
        parse_ts(value)
        return value

    @model_validator(mode="after")
    def _reject_payloads(self) -> TrajectoryHop:
        reject_raw_payloads(_own_fields(self))
        return self

    @model_serializer(mode="wrap")
    def _always_carry_parent(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # The root's ``parent: null`` is a statement ("this hop is the chain
        # root"), not an unset option; ``exclude_none`` must not erase it.
        out: dict[str, Any] = handler(self)
        out["parent"] = self.parent
        return out


class TrajectoryFinding(BaseModel):
    """One defect the walk found, naming the hop by its list index."""

    model_config = ConfigDict(frozen=True)

    #: Index into ``trajectory``; ``None`` for a chain-level finding.
    hop_index: int | None
    code: FindingCode
    #: ``error`` fails verification; ``warning`` is recorded, never fatal.
    severity: Literal["error", "warning"]
    message: str


class TrajectoryReport(BaseModel):
    """The result of walking a trajectory chain offline.

    ``acyclic`` / ``no_broken_parent`` / ``monotonic`` are the three spec
    properties; ``complete`` says whether some actuation hop's ancestry
    reaches a perception root — informational, because a partial chain
    (perception → decision, nothing actuated) is legitimate evidence.
    """

    model_config = ConfigDict(frozen=True)

    hop_count: int
    acyclic: bool
    no_broken_parent: bool
    monotonic: bool
    complete: bool
    findings: list[TrajectoryFinding] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no finding has ``error`` severity."""
        return not any(f.severity == "error" for f in self.findings)


def _error(index: int | None, code: FindingCode, message: str) -> TrajectoryFinding:
    return TrajectoryFinding(hop_index=index, code=code, severity="error", message=message)


def _link_findings(
    index: int,
    hop: TrajectoryHop,
    earlier: dict[str, int],
    last_seen: dict[str, int],
    hops: Sequence[TrajectoryHop],
) -> tuple[list[TrajectoryFinding], int | None]:
    """Check one hop's parent link; return findings and the resolved parent index."""
    if hop.parent is None:
        if index == 0:
            return [], None
        return [
            _error(
                index,
                "missing_parent",
                f"hop {index} ({hop.stage}) has no parent; only the first hop may be "
                "the chain root",
            )
        ], None
    if hop.parent == hop.output_digest:
        return [
            _error(index, "self_reference", f"hop {index} ({hop.stage}) cites its own output")
        ], None
    if hop.parent in earlier:
        return _order_findings(index, hop, earlier[hop.parent], hops), earlier[hop.parent]
    if last_seen.get(hop.parent, -1) > index:
        return [
            _error(
                index,
                "forward_reference",
                f"hop {index} ({hop.stage}) cites the output of a later hop; "
                "provenance must point backwards",
            )
        ], None
    return [
        _error(
            index,
            "broken_parent",
            f"hop {index} ({hop.stage}) cites parent {hop.parent} which is no earlier hop's output",
        )
    ], None


def _order_findings(
    index: int, hop: TrajectoryHop, parent_index: int, hops: Sequence[TrajectoryHop]
) -> list[TrajectoryFinding]:
    """Time and stage ordering between a hop and its resolved parent."""
    parent = hops[parent_index]
    findings: list[TrajectoryFinding] = []
    if parse_ts(hop.ts) < parse_ts(parent.ts):
        findings.append(
            _error(
                index,
                "ts_regression",
                f"hop {index} ({hop.stage}, ts={hop.ts}) precedes its parent hop "
                f"{parent_index} (ts={parent.ts})",
            )
        )
    # A new perception after an actuation is the next turn of a closed loop,
    # not a regression; anything else stepping backwards is recorded, not fatal.
    if hop.stage != "perception" and _STAGE_RANK[hop.stage] < _STAGE_RANK[parent.stage]:
        findings.append(
            TrajectoryFinding(
                hop_index=index,
                code="stage_regression",
                severity="warning",
                message=(
                    f"hop {index} ({hop.stage}) derives from a later-stage hop "
                    f"{parent_index} ({parent.stage})"
                ),
            )
        )
    return findings


def _perception_rooted(parents: Sequence[int | None], hops: Sequence[TrajectoryHop]) -> list[bool]:
    """For every hop, whether its resolved ancestry ends at a perception root.

    One forward pass: resolved links only ever point to earlier indices, so
    ``root[i] = root[parents[i]]`` is already known when hop ``i`` is reached
    — O(n) overall, never a per-hop walk back. A hop whose parent did not
    resolve is its own (non-perception-root) end of ancestry.
    """
    rooted: list[bool] = []
    for index, parent_index in enumerate(parents):
        if parent_index is None:
            hop = hops[index]
            rooted.append(hop.stage == "perception" and hop.parent is None)
        elif parent_index < index:
            rooted.append(rooted[parent_index])
        else:  # pragma: no cover - resolution never points forward
            rooted.append(False)
    return rooted


def walk_trajectory(hops: Sequence[TrajectoryHop]) -> TrajectoryReport:
    """Walk a trajectory chain offline and report every defect by hop index.

    Pure: reads the hops, touches nothing else. Parent resolution only ever
    looks *backwards*, so the walk terminates on any input — a cycle shows up
    as a ``forward_reference`` or ``self_reference`` finding, never as a hang.
    Linear in the hop count; a chain over :data:`MAX_HOPS` is not walked and
    reports a single ``chain_too_long`` error (every verdict false).
    """
    if not hops:
        return TrajectoryReport(
            hop_count=0,
            acyclic=True,
            no_broken_parent=False,
            monotonic=True,
            complete=False,
            findings=[_error(None, "empty_chain", "trajectory is present but has no hops")],
        )

    if len(hops) > MAX_HOPS:
        return TrajectoryReport(
            hop_count=len(hops),
            acyclic=False,
            no_broken_parent=False,
            monotonic=False,
            complete=False,
            findings=[
                _error(
                    MAX_HOPS,
                    "chain_too_long",
                    f"trajectory exceeds {MAX_HOPS} hops; refusing to walk it",
                )
            ],
        )

    findings: list[TrajectoryFinding] = []
    earlier: dict[str, int] = {}
    parents: list[int | None] = []
    last_seen = {hop.output_digest: index for index, hop in enumerate(hops)}
    for index, hop in enumerate(hops):
        hop_findings, parent_index = _link_findings(index, hop, earlier, last_seen, hops)
        findings.extend(hop_findings)
        parents.append(parent_index)
        if hop.output_digest in earlier:
            findings.append(
                _error(
                    index,
                    "duplicate_output",
                    f"hop {index} ({hop.stage}) repeats the output of hop "
                    f"{earlier[hop.output_digest]}; parent resolution is ambiguous",
                )
            )
        else:
            earlier[hop.output_digest] = index

    codes = {f.code for f in findings}
    rooted = _perception_rooted(parents, hops)
    complete = any(hop.stage == "actuation" and rooted[i] for i, hop in enumerate(hops))
    return TrajectoryReport(
        hop_count=len(hops),
        acyclic=not (codes & _CYCLE_CODES),
        no_broken_parent=not (codes & _BROKEN_CODES),
        monotonic="ts_regression" not in codes,
        complete=complete,
        findings=findings,
    )


__all__ = [
    "MAX_HOPS",
    "FindingCode",
    "TrajectoryFinding",
    "TrajectoryHop",
    "TrajectoryReport",
    "TrajectoryStage",
    "walk_trajectory",
]
