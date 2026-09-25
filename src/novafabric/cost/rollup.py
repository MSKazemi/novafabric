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

"""Acted-as cost rollup — ADR-0146 D1/P2 (NF-142), report-only.

Rolls the per-agent spend of an NF-141 ``cost_attribution`` facet **up** the
acted-as delegation graph (ADR-0106 §NF-084, :mod:`novafabric.trust.delegation`)
so every granter carries its grantees' spend: for each hop, ``self_cost`` (what
that principal was attributed) and ``subtree_cost`` (self plus every
descendant), then a conservation block comparing the root subtree(s) with the
run total.

Design commitments:

- **Report-only (deviation from spec §4.2).** The spec sketches a
  ``facets.cost_rollup`` capsule facet. That name is not in the closed facet
  registry (ADR-0196 D2), so this module never writes a facet: it returns a
  :class:`RollupReport` the CLI prints. A rollup is fully re-derivable from two
  inputs the operator already holds, so persisting it adds no evidence.
- **Record-only (ADR-0146 I-4).** A large subtree is a fact, never a verdict:
  there is no threshold, quota, or over-budget field.
- **Exact money.** Amounts are :class:`~decimal.Decimal` major units derived
  from the facet's integer minor units, summed under a local context that
  traps ``Inexact`` — a sum that could not be represented exactly raises
  instead of rounding. Conservation is exact equality, never ``± epsilon``.
- **Fail-open, never crash on bad graphs.** A missing chain degrades to
  ``basis: partial``; a cycle, a self-grant, a broken hop linkage, or a
  principal with two granters becomes a :class:`RollupFinding` and the graph
  is repaired deterministically (documented per case) rather than raising.
- **Bounded.** Grants, chains, principals and tree depth are capped; the walk is
  iterative (no recursion), so a hostile document cannot exhaust the stack.
- **Unattributed cost is explicit.** Spend the capture could not tie to an
  agent, and spend of agents that appear in no delegation chain, are reported
  as their own conservation lines — never folded into a root or dropped.
- **Linkage only, not signatures.** The rollup reads grant *identities*; it
  does not re-verify Ed25519 signatures (that needs trusted roots — use
  :func:`novafabric.trust.delegation.verify_delegation_chain`). The report says
  so in ``signatures_verified: false``.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, Inexact, localcontext
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from novafabric.cost.attribution import (
    FACET_NAME,
    CostAttributionFacet,
    verify_conservation,
)

__all__ = [
    "MAX_CHAINS",
    "MAX_DEPTH",
    "MAX_GRANTS",
    "MAX_PRINCIPALS",
    "RECORD_ONLY_LINE",
    "GrantEdge",
    "RollupBoundsError",
    "RollupConservation",
    "RollupError",
    "RollupFinding",
    "RollupHop",
    "RollupInputError",
    "RollupReport",
    "UnchainedAgent",
    "build_rollup",
    "parse_attribution",
    "parse_delegation",
]

#: Hard caps on the delegation document. A real acted-as graph is a handful of
#: hops; these exist so a hostile or runaway file cannot make the walk
#: unbounded.
MAX_GRANTS = 10_000
MAX_CHAINS = 1_000
MAX_PRINCIPALS = 10_000
MAX_DEPTH = 256
#: Upper bound on any single minor-unit amount (10^30). Together with
#: MAX_PRINCIPALS it keeps every sum inside the 64-digit exact context.
_MAX_MINOR = 10**30
_DECIMAL_PRECISION = 64

RECORD_ONLY_LINE = (
    "Record-only: NovaFabric attributes cost a run already incurred; it does not "
    "enforce a budget, throttle an agent, or block a workload (ADR-0146 I-4)."
)

#: ISO-4217 currencies whose minor unit is not 1/100. Every other code is
#: treated as two-decimal. Static on purpose: only exponent 0 and 3 exist
#: outside the default, and this table changes on the scale of decades.
_ZERO_DECIMAL = frozenset(
    {
        "BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG",
        "RWF", "UGX", "UYI", "VND", "VUV", "XAF", "XOF", "XPF",
    }
)  # fmt: skip
_THREE_DECIMAL = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})

FindingCode = Literal[
    "no_chain",
    "broken_linkage",
    "self_delegation",
    "cycle",
    "multiple_granters",
    "unchained_agent",
    "no_run_total",
    "attribution_not_conserved",
]


# ── Errors ────────────────────────────────────────────────────────────────


class RollupError(Exception):
    """Base class for inputs the rollup refuses (the CLI maps these to exit 2)."""


class RollupInputError(RollupError):
    """A delegation or attribution document is malformed."""


class RollupBoundsError(RollupError):
    """A document exceeds a hard cap (grants, chains, principals, depth, amount)."""


# ── Models ────────────────────────────────────────────────────────────────


class GrantEdge(BaseModel):
    """The identity projection of one :class:`~novafabric.trust.delegation.Grant`.

    Only the fields the rollup needs; keys are kept as opaque values purely for
    the linkage comparison and never interpreted.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    granter_id: str = Field(min_length=1, max_length=512)
    grantee_id: str = Field(min_length=1, max_length=512)
    granter_public_key: Any = None
    grantee_public_key: Any = None


class RollupFinding(BaseModel):
    """A structural observation about the inputs — reported, never raised."""

    model_config = ConfigDict(frozen=True)

    code: FindingCode
    detail: str
    principals: list[str] = Field(default_factory=list)


class RollupHop(BaseModel):
    """One principal in the acted-as forest (spec §4.2 ``hops[]``).

    ``self_cost`` is ``None`` when the principal carries no attributed cost —
    typically the human root of the chain, or an agent whose spend the capture
    could not attribute. Absent is not zero (NF-141 I-4); ``subtree_cost`` sums
    only attributed figures.
    """

    model_config = ConfigDict(frozen=True)

    agent_id: str
    granter: str | None
    depth: int
    self_cost: Decimal | None
    subtree_cost: Decimal
    grantees: list[str]


class UnchainedAgent(BaseModel):
    """An attributed agent that appears in no delegation grant."""

    model_config = ConfigDict(frozen=True)

    agent_id: str
    self_cost: Decimal | None


class RollupConservation(BaseModel):
    """Root subtree(s) versus the run total, with every gap named.

    Identity (always true by construction, re-checked): ``run_total_cost ==
    root_subtree_cost + unchained_cost + unattributed_cost`` whenever a run
    total exists. ``ok`` is the spec's claim — the delegation roots account for
    the *whole* run total, exactly.
    """

    model_config = ConfigDict(frozen=True)

    currency: str | None
    roots: list[str]
    root_subtree_cost: Decimal
    run_total_cost: Decimal | None
    unchained_cost: Decimal
    unattributed_cost: Decimal | None
    ok: bool


class RollupReport(BaseModel):
    """The NF-142 rollup: report-only, deterministic, never persisted as a facet."""

    model_config = ConfigDict(frozen=True)

    over: str = "delegation-document"
    basis: Literal["measured", "apportioned", "partial"]
    signatures_verified: bool = False
    hops: list[RollupHop]
    unchained: list[UnchainedAgent]
    conservation: RollupConservation
    findings: list[RollupFinding]
    record_only: str = RECORD_ONLY_LINE


# ── Parsing ───────────────────────────────────────────────────────────────


def _parse_edges(raw: object, where: str) -> list[GrantEdge]:
    if not isinstance(raw, list):
        raise RollupInputError(f"{where}: `grants` must be an array")
    edges: list[GrantEdge] = []
    for i, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise RollupInputError(f"{where}: grant {i} must be an object")
        try:
            edges.append(GrantEdge.model_validate(dict(item)))
        except ValidationError as exc:
            raise RollupInputError(f"{where}: grant {i} is malformed: {exc}") from exc
    return edges


def parse_delegation(doc: object) -> list[list[GrantEdge]]:
    """Parse a delegation document into ordered chains of grant edges.

    Accepted shapes (both may appear together):

    - ``{"grants": [...]}`` — one :class:`~novafabric.trust.delegation.DelegationChain`
      (ordered, root first);
    - ``{"chains": [{"grants": [...]}, ...]}`` — several chains whose union forms
      the acted-as tree (one chain per root-to-leaf path).

    An empty document (no grants anywhere) returns ``[]`` — the caller degrades
    to ``basis: partial``.

    Raises:
        RollupInputError: on a non-object document or malformed grants.
        RollupBoundsError: past :data:`MAX_CHAINS` or :data:`MAX_GRANTS`.
    """
    if not isinstance(doc, Mapping):
        raise RollupInputError("delegation document must be a JSON object")
    chains: list[list[GrantEdge]] = []
    if "grants" in doc:
        chains.append(_parse_edges(doc["grants"], "delegation"))
    raw_chains = doc.get("chains", [])
    if not isinstance(raw_chains, list):
        raise RollupInputError("delegation: `chains` must be an array")
    if len(raw_chains) > MAX_CHAINS:
        raise RollupBoundsError(f"delegation has {len(raw_chains)} chains (cap {MAX_CHAINS})")
    for i, chain in enumerate(raw_chains):
        if not isinstance(chain, Mapping) or "grants" not in chain:
            raise RollupInputError(f"delegation: chain {i} must be an object with `grants`")
        chains.append(_parse_edges(chain["grants"], f"chain {i}"))
    total = sum(len(c) for c in chains)
    if total > MAX_GRANTS:
        raise RollupBoundsError(f"delegation has {total} grants (cap {MAX_GRANTS})")
    return [c for c in chains if c]


def parse_attribution(doc: object) -> CostAttributionFacet:
    """Parse an NF-141 facet from a bare facet, a ``facets`` wrapper, or a manifest.

    Raises:
        RollupInputError: when no ``cost_attribution`` facet is present or it
            does not validate.
        RollupBoundsError: on an amount past the exact-arithmetic cap.
    """
    if not isinstance(doc, Mapping):
        raise RollupInputError("cost attribution document must be a JSON/YAML object")
    raw: object = doc
    facets = doc.get("facets")
    if isinstance(facets, Mapping):
        if FACET_NAME not in facets:
            raise RollupInputError(f"no facets.{FACET_NAME} in the document")
        raw = facets[FACET_NAME]
    elif FACET_NAME in doc:
        raw = doc[FACET_NAME]
    if not isinstance(raw, Mapping) or "run_total" not in raw:
        raise RollupInputError(f"no {FACET_NAME} facet found (expected `run_total` + `by_agent`)")
    try:
        facet = CostAttributionFacet.model_validate(dict(raw))
    except ValidationError as exc:
        raise RollupInputError(f"{FACET_NAME} facet is malformed: {exc}") from exc
    if len(facet.by_agent) > MAX_PRINCIPALS:
        raise RollupBoundsError(f"facet has {len(facet.by_agent)} agents (cap {MAX_PRINCIPALS})")
    amounts = [a.cost.amount_minor for a in facet.by_agent if a.cost is not None]
    if facet.run_total.cost is not None:
        amounts.append(facet.run_total.cost.amount_minor)
    if any(a >= _MAX_MINOR for a in amounts):
        raise RollupBoundsError(f"an amount exceeds the exact-arithmetic cap ({_MAX_MINOR})")
    return facet


# ── Money helpers ─────────────────────────────────────────────────────────


def _exponent(currency: str) -> int:
    if currency in _ZERO_DECIMAL:
        return 0
    if currency in _THREE_DECIMAL:
        return 3
    return 2


def _to_major(amount_minor: int, currency: str) -> Decimal:
    """Exact minor → major conversion (``scaleb`` only shifts the exponent)."""
    return Decimal(amount_minor).scaleb(-_exponent(currency))


def _zero(currency: str | None) -> Decimal:
    return Decimal(0).scaleb(-_exponent(currency)) if currency else Decimal(0)


# ── Graph construction ────────────────────────────────────────────────────


def _collect_edges(
    chains: list[list[GrantEdge]], findings: list[RollupFinding]
) -> set[tuple[str, str]]:
    """Linkage-check each chain and return the valid (granter, grantee) edges.

    A hop whose granter is not the previous hop's grantee (identity, or public
    key when both sides carry one) breaks the chain: that hop and every later
    hop of the same chain are dropped — the ADR-0106 verifier would reject
    them, so they confer no acted-as authority to roll cost along.
    """
    edges: set[tuple[str, str]] = set()
    for ci, chain in enumerate(chains):
        prev: GrantEdge | None = None
        for hi, grant in enumerate(chain):
            if prev is not None:
                id_break = grant.granter_id != prev.grantee_id
                key_break = (
                    grant.granter_public_key is not None
                    and prev.grantee_public_key is not None
                    and grant.granter_public_key != prev.grantee_public_key
                )
                if id_break or key_break:
                    findings.append(
                        RollupFinding(
                            code="broken_linkage",
                            detail=(
                                f"chain {ci} hop {hi}: granter {grant.granter_id!r} is not the "
                                f"previous grantee {prev.grantee_id!r}"
                                + (" (public key mismatch)" if key_break and not id_break else "")
                                + f"; hops {hi}..{len(chain) - 1} dropped"
                            ),
                            principals=sorted({grant.granter_id, prev.grantee_id}),
                        )
                    )
                    break
            if grant.granter_id == grant.grantee_id:
                findings.append(
                    RollupFinding(
                        code="self_delegation",
                        detail=(
                            f"chain {ci} hop {hi}: {grant.granter_id!r} grants to itself; "
                            "edge ignored"
                        ),
                        principals=[grant.granter_id],
                    )
                )
            else:
                edges.add((grant.granter_id, grant.grantee_id))
            prev = grant
    return edges


def _parent_map(edges: set[tuple[str, str]], findings: list[RollupFinding]) -> dict[str, str]:
    """Reduce the edge set to one granter per principal (a functional graph).

    A principal granted by two different granters would be counted in two
    subtrees, breaking conservation. The lexicographically smallest granter is
    kept (deterministic) and the rest are reported as ``multiple_granters``.
    """
    granters: dict[str, list[str]] = {}
    for granter, grantee in edges:
        granters.setdefault(grantee, []).append(granter)
    parent: dict[str, str] = {}
    for grantee in sorted(granters):
        options = sorted(granters[grantee])
        parent[grantee] = options[0]
        if len(options) > 1:
            findings.append(
                RollupFinding(
                    code="multiple_granters",
                    detail=(
                        f"{grantee!r} is granted by {options}; its subtree is rolled up "
                        f"under {options[0]!r} only so no cost is counted twice"
                    ),
                    principals=[grantee, *options],
                )
            )
    return parent


def _strongly_connected(edges: set[tuple[str, str]]) -> list[list[str]]:
    """Non-trivial strongly connected components (iterative Tarjan, O(V+E)).

    Iterative so a long adversarial chain cannot hit the recursion limit.
    Nodes and successors are visited in sorted order, so the result is
    deterministic. Self-loops never reach here (filtered as ``self_delegation``).
    """
    succ: dict[str, list[str]] = {}
    for granter, grantee in edges:
        succ.setdefault(granter, []).append(grantee)
        succ.setdefault(grantee, [])
    for targets in succ.values():
        targets.sort()
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0
    for root in sorted(succ):
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, pos = work.pop()
            if pos == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            targets = succ[node]
            if pos < len(targets):
                work.append((node, pos + 1))
                nxt = targets[pos]
                if nxt not in index:
                    work.append((nxt, 0))
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
                continue
            if work:
                parent_node = work[-1][0]
                low[parent_node] = min(low[parent_node], low[node])
            if low[node] == index[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    components.append(sorted(component))
    return sorted(components)


def _drop_cyclic_edges(
    edges: set[tuple[str, str]], findings: list[RollupFinding]
) -> set[tuple[str, str]]:
    """Report every delegation cycle and drop the grants that form it.

    Cycles are detected on the *full* grant graph (before any one-granter
    reduction, which could otherwise hide a cycle). An edge whose endpoints
    share a cycle carries no acted-as authority to roll cost along — there is
    no root it leads back to — so it is ignored; each cycle member then rolls
    up under a granter outside the cycle if it has one, or as its own root.
    Edges into or out of the cycle are kept.
    """
    components = _strongly_connected(edges)
    member_of = {m: i for i, comp in enumerate(components) for m in comp}
    for comp in components:
        findings.append(
            RollupFinding(
                code="cycle",
                detail=(
                    f"delegation cycle through {comp}; the grants inside the cycle were "
                    "ignored and cost is rolled up only along non-cyclic grants"
                ),
                principals=comp,
            )
        )
    return {(a, b) for a, b in edges if not (a in member_of and member_of.get(b) == member_of[a])}


# ── Rollup ────────────────────────────────────────────────────────────────


def _self_costs(facet: CostAttributionFacet, currency: str | None) -> dict[str, Decimal | None]:
    """Per-agent attributed cost in major units (``None`` when unattributed).

    Raises:
        RollupInputError: on an agent costed in a different currency, or a
            duplicated ``agent_id`` (either would make a sum meaningless).
    """
    out: dict[str, Decimal | None] = {}
    for agent in facet.by_agent:
        if agent.agent_id in out:
            raise RollupInputError(f"agent {agent.agent_id!r} appears twice in by_agent")
        if agent.cost is None:
            out[agent.agent_id] = None
            continue
        if currency is not None and agent.cost.currency != currency:
            raise RollupInputError(
                f"agent {agent.agent_id!r} is costed in {agent.cost.currency} but the "
                f"run total is {currency}; cross-currency rollup is refused"
            )
        out[agent.agent_id] = _to_major(agent.cost.amount_minor, agent.cost.currency)
    return out


def _currency(facet: CostAttributionFacet) -> str | None:
    if facet.run_total.cost is not None:
        return facet.run_total.cost.currency
    codes = sorted({a.cost.currency for a in facet.by_agent if a.cost is not None})
    if len(codes) > 1:
        raise RollupInputError(f"agents are costed in several currencies {codes}")
    return codes[0] if codes else None


def _depths(nodes: list[str], parent: dict[str, str]) -> dict[str, int]:
    """Iterative depth of each node in the (acyclic) forest, capped."""
    depth: dict[str, int] = {}
    for start in nodes:
        chain: list[str] = []
        node = start
        while node not in depth and node in parent:
            chain.append(node)
            if len(chain) > MAX_DEPTH:
                raise RollupBoundsError(f"delegation depth exceeds {MAX_DEPTH}")
            node = parent[node]
        base = depth.setdefault(node, 0)
        for offset, member in enumerate(reversed(chain), start=1):
            depth[member] = base + offset
            if depth[member] > MAX_DEPTH:
                raise RollupBoundsError(f"delegation depth exceeds {MAX_DEPTH}")
    return depth


def build_rollup(chains: list[list[GrantEdge]], facet: CostAttributionFacet) -> RollupReport:
    """Roll the facet's per-agent cost up the acted-as forest.

    Deterministic: hops are emitted in pre-order from the sorted roots with
    sorted grantees; unchained agents and findings are sorted. Structural
    defects become findings; only malformed/oversized input raises.

    Raises:
        RollupInputError: on inconsistent currencies or duplicate agents.
        RollupBoundsError: when principals or depth exceed their caps.
    """
    findings: list[RollupFinding] = []
    currency = _currency(facet)
    self_cost = _self_costs(facet, currency)

    if not chains:
        findings.append(
            RollupFinding(
                code="no_chain",
                detail="no delegation grants supplied; nothing rolls up (flat NF-141 split stands)",
            )
        )
    all_edges = _collect_edges(chains, findings)
    # Membership comes from every linked grant, so a principal whose only
    # grants formed a cycle is still "in the chain" (a root), not unchained.
    in_graph = {p for edge in all_edges for p in edge}
    edges = _drop_cyclic_edges(all_edges, findings)
    parent = _parent_map(edges, findings)

    if len(in_graph) > MAX_PRINCIPALS:
        raise RollupBoundsError(
            f"delegation names {len(in_graph)} principals (cap {MAX_PRINCIPALS})"
        )
    nodes = sorted(in_graph)
    depth = _depths(nodes, parent)
    children: dict[str, list[str]] = {n: [] for n in nodes}
    for child, granter in parent.items():
        children[granter].append(child)
    for kids in children.values():
        kids.sort()

    zero = _zero(currency)
    with localcontext() as ctx:
        ctx.prec = _DECIMAL_PRECISION
        ctx.traps[Inexact] = True
        subtree: dict[str, Decimal] = {}
        # Deepest first, so every child is final before its granter reads it.
        for node in sorted(nodes, key=lambda n: (-depth[n], n)):
            own = self_cost.get(node)
            total = zero if own is None else own
            for kid in children[node]:
                total += subtree[kid]
            subtree[node] = total

        roots = [n for n in nodes if n not in parent]
        hops: list[RollupHop] = []
        stack = list(reversed(roots))
        while stack:
            node = stack.pop()
            hops.append(
                RollupHop(
                    agent_id=node,
                    granter=parent.get(node),
                    depth=depth[node],
                    self_cost=self_cost.get(node),
                    subtree_cost=subtree[node],
                    grantees=list(children[node]),
                )
            )
            stack.extend(reversed(children[node]))

        unchained = [
            UnchainedAgent(agent_id=a, self_cost=self_cost[a])
            for a in sorted(self_cost)
            if a not in in_graph
        ]
        if unchained and chains:
            findings.append(
                RollupFinding(
                    code="unchained_agent",
                    detail="attributed agents that appear in no delegation grant; their cost "
                    "is reported as unchained, not rolled into a root",
                    principals=[u.agent_id for u in unchained],
                )
            )
        root_sum = sum((subtree[r] for r in roots), zero)
        unchained_sum = sum((u.self_cost for u in unchained if u.self_cost is not None), zero)

        run_total: Decimal | None = None
        unattributed: Decimal | None = None
        if facet.run_total.cost is not None:
            run_total = _to_major(facet.run_total.cost.amount_minor, facet.run_total.cost.currency)
            unattributed = run_total - root_sum - unchained_sum
        else:
            findings.append(
                RollupFinding(
                    code="no_run_total",
                    detail="the facet records no run-total cost; conservation cannot be checked",
                )
            )

    if facet.conservation and not verify_conservation(facet):
        findings.append(
            RollupFinding(
                code="attribution_not_conserved",
                detail=f"the input {FACET_NAME} facet fails its own NF-141 conservation re-check",
            )
        )

    ok = run_total is not None and root_sum == run_total
    conservation = RollupConservation(
        currency=currency,
        roots=roots,
        root_subtree_cost=root_sum,
        run_total_cost=run_total,
        unchained_cost=unchained_sum,
        unattributed_cost=unattributed,
        ok=ok,
    )

    basis: Literal["measured", "apportioned", "partial"]
    if findings or not ok:
        basis = "partial"
    elif all(a.basis == "measured" for a in facet.by_agent):
        basis = "measured"
    else:
        basis = "apportioned"

    findings.sort(key=lambda f: (f.code, f.principals, f.detail))
    return RollupReport(
        basis=basis,
        hops=hops,
        unchained=unchained,
        conservation=conservation,
        findings=findings,
    )
