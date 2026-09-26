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

"""Liability-attribution chain — ADR-0170 P2 (NF-383, D3).

``facets.risk_transfer.liability_chain`` is an ordered list of
``{role, party_ref, acted_as_ref, basis_ref, contribution_marker}`` recording
**who was attributed** a part in a run — principal, deployer, operator,
vendor, model provider, integrator, agent, third party — and on whose
assertion. It is attribution *evidence*: the record a court or insurer reasons
over under UETA §9 / agency-law attribution and the strict-liability PLD, in a
regime where the EU AI Liability Directive was withdrawn.

It is **never a finding of fault** (I-4). There is no fault, liability-share or
verdict field, and :func:`~novafabric.risk_transfer._guard.reject_determination_fields`
refuses any key shaped like one, including in ``extra`` fields.

``contribution_marker`` records the *state of an assertion*, not its truth:

- ``recorded`` — a named source (``basis_ref``) asserted this party contributed;
- ``disputed`` — a contribution was asserted and is contested; the dispute is
  itself evidence, and it too must name its ``basis_ref``;
- ``none`` — the party is in the chain (e.g. as the principal an agent acted
  for) with no contribution asserted.

``acted_as_ref`` extends the ADR-0106 delegation ``acted_as`` edge **by
reference**: it names the ``party_ref`` of another chain member this party
acted for. A dangling, self-referencing, or cyclic ``acted_as`` edge is
refused — a chain that cannot be walked is not attribution evidence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, field_validator, model_validator

from novafabric.risk_transfer._guard import reject_determination_fields, validate_ref

#: Roles in the attribution chain (ADR-0170 D3 — fixed set).
LiabilityRole = Literal[
    "principal",
    "deployer",
    "operator",
    "vendor",
    "model_provider",
    "integrator",
    "agent",
    "third_party",
]

#: State of a contribution assertion — never a finding (D3).
ContributionMarker = Literal["recorded", "disputed", "none"]

#: Longest chain accepted. A delegation chain of this length is already far
#: beyond any real deployment; the cap bounds the cycle walk.
MAX_CHAIN = 64


class UnsourcedContributionError(Exception):
    """Raised when a ``recorded``/``disputed`` marker names no ``basis_ref``.

    D3: the marker records that a contribution was *asserted by a named
    source*. With no source it is NovaFabric's own assertion — the one thing
    this record must never contain.
    """


class InvalidLiabilityChainError(Exception):
    """Raised on a chain that cannot be walked as attribution evidence.

    Covers an empty or over-long chain, a duplicate ``(role, party_ref)``
    entry, and an ``acted_as_ref`` that is dangling, self-referencing or part
    of a cycle.
    """


class LiabilityEdge(BaseModel):
    """One attributed party in the chain (NF-383). Attribution, never fault."""

    model_config = ConfigDict(extra="allow")

    role: LiabilityRole
    #: Digest identifying the party — never a name, never an account.
    party_ref: str
    #: ``party_ref`` of the chain member this party acted as/for (ADR-0106).
    acted_as_ref: str | None = None
    #: Digest of the source asserting the contribution (contract, ToS,
    #: statute citation document, a party's own statement).
    basis_ref: str | None = None
    contribution_marker: ContributionMarker = "none"

    @field_validator("party_ref", "acted_as_ref", "basis_ref")
    @classmethod
    def _check_refs(cls, value: str | None) -> str | None:
        return validate_ref(value)

    @model_validator(mode="after")
    def _check_edge(self) -> LiabilityEdge:
        if self.contribution_marker != "none" and self.basis_ref is None:
            raise UnsourcedContributionError(
                f"{self.role} entry is marked {self.contribution_marker!r} with no "
                "basis_ref; a contribution marker records an assertion by a named "
                "source, never NovaFabric's own finding (ADR-0170 D3)"
            )
        if self.acted_as_ref is not None and self.acted_as_ref == self.party_ref:
            raise InvalidLiabilityChainError(
                f"{self.role} entry acts as itself; an acted_as edge must name another chain member"
            )
        reject_determination_fields(self.model_dump(mode="json"))
        return self


def check_chain(edges: list[LiabilityEdge]) -> list[LiabilityEdge]:
    """Enforce the chain-level invariants of a liability chain.

    Raises:
        InvalidLiabilityChainError: empty/over-long, duplicate entry, or an
            ``acted_as_ref`` that is dangling or forms a cycle.
    """
    if not edges:
        raise InvalidLiabilityChainError(
            "an empty liability chain is not emitted; absent means not recorded"
        )
    if len(edges) > MAX_CHAIN:
        raise InvalidLiabilityChainError(
            f"liability chain has {len(edges)} entries (cap {MAX_CHAIN})"
        )
    seen: set[tuple[str, str]] = set()
    parties = {edge.party_ref for edge in edges}
    for index, edge in enumerate(edges):
        key = (edge.role, edge.party_ref)
        if key in seen:
            raise InvalidLiabilityChainError(
                f"entry [{index}] repeats role {edge.role!r} for the same party_ref"
            )
        seen.add(key)
        if edge.acted_as_ref is not None and edge.acted_as_ref not in parties:
            raise InvalidLiabilityChainError(
                f"entry [{index}] ({edge.role}) acted_as_ref names no party in the "
                "chain; the delegation edge it extends must be walkable (ADR-0106)"
            )
    # Cycle check over the party graph. A party may hold several roles, so its
    # outgoing edges are the union over its entries.
    graph: dict[str, set[str]] = {}
    for edge in edges:
        if edge.acted_as_ref is not None:
            graph.setdefault(edge.party_ref, set()).add(edge.acted_as_ref)
    _reject_cycles(graph)
    return edges


def _reject_cycles(graph: Mapping[str, set[str]]) -> None:
    """Iterative three-colour DFS; raise on a back edge (bounded by MAX_CHAIN)."""
    state: dict[str, int] = {}  # 1 = on stack, 2 = done
    for start in sorted(graph):
        if state.get(start):
            continue
        stack: list[tuple[str, list[str]]] = [(start, sorted(graph.get(start, ())))]
        state[start] = 1
        while stack:
            node, pending = stack[-1]
            if not pending:
                state[node] = 2
                stack.pop()
                continue
            nxt = pending.pop()
            mark = state.get(nxt)
            if mark == 1:
                raise InvalidLiabilityChainError(
                    "acted_as edges form a cycle; a delegation chain must terminate"
                )
            if mark is None:
                state[nxt] = 1
                stack.append((nxt, sorted(graph.get(nxt, ()))))


#: The facet field type: a validated, ordered, non-empty chain.
LiabilityChain = Annotated[list[LiabilityEdge], AfterValidator(check_chain)]


def build_liability_chain(
    entries: Iterable[LiabilityEdge | Mapping[str, Any]],
) -> list[LiabilityEdge] | None:
    """Build a validated liability chain, or ``None`` when there is nothing to record.

    Fail-open (I-3): no entries yields ``None`` — never an empty chain, which
    would read as "recorded, nobody attributed". Order is preserved exactly:
    the chain is ordered evidence and NovaFabric does not re-rank it.

    Raises:
        UnsourcedContributionError, InvalidLiabilityChainError,
        InvalidReferenceError, DeterminationFieldRejectedError: on bad input.
    """
    edges = [
        entry if isinstance(entry, LiabilityEdge) else LiabilityEdge.model_validate(entry)
        for entry in entries
    ]
    if not edges:
        return None
    return check_chain(edges)


def attributed_parties(chain: Iterable[LiabilityEdge]) -> list[str]:
    """Return ``party_ref``s whose contribution was asserted (recorded or disputed).

    A read-back of recorded assertions, in chain order and de-duplicated — not
    a list of parties at fault.
    """
    out: list[str] = []
    for edge in chain:
        if edge.contribution_marker != "none" and edge.party_ref not in out:
            out.append(edge.party_ref)
    return out


__all__ = [
    "MAX_CHAIN",
    "ContributionMarker",
    "InvalidLiabilityChainError",
    "LiabilityChain",
    "LiabilityEdge",
    "LiabilityRole",
    "UnsourcedContributionError",
    "attributed_parties",
    "build_liability_chain",
    "check_chain",
]
