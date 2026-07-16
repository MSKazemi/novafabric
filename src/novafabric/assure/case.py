"""Machine-checkable assurance-case argument graph (ADR-0166 D1, first slice).

An assurance case is a GSN/SACM/CAE node graph — ``goal / strategy / solution /
context / assumption / justification`` — whose ``solution`` nodes bind to sealed
capsule roots **by digest only** (never clause bodies, findings, or PII). This
module models that graph and validates its structural invariants:

* exactly one **top goal** (a ``goal`` node no other node supports),
* the ``supported_by`` graph is **acyclic**,
* **no orphan** — every node is reachable from the top goal,
* node ids are unique and every ``supported_by`` reference resolves.

A ``solution`` node with no resolvable evidence is reported as an
``unsupported_leaf`` — recorded, **not fatal** — so an in-progress argument stays
valid while honestly flagging its gaps.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class NodeType(str, Enum):
    goal = "goal"
    strategy = "strategy"
    solution = "solution"
    context = "context"
    assumption = "assumption"
    justification = "justification"


class EvidenceRef(BaseModel):
    """A reference from a solution node to a sealed capsule root — digest only."""

    ref: str  # capsule root / bundle identifier (a reference, never a body)
    digest: str  # content digest that binds the reference (e.g. sha256 hex)


class AssuranceNode(BaseModel):
    id: str
    type: NodeType
    statement: str  # the argument text — never findings or PII
    supported_by: list[str] = []  # child node ids
    evidence_refs: list[EvidenceRef] = []  # meaningful for `solution` nodes


class AssuranceCase(BaseModel):
    case_id: str
    nodes: list[AssuranceNode] = []


class CaseValidation(BaseModel):
    """Result of validating an assurance case's structure and evidence resolution."""

    valid: bool  # True when there are no fatal structural errors
    errors: list[str] = []  # fatal: dup id, unknown ref, top-goal count, cycle, orphan
    top_goal_id: str | None = None
    unsupported_leaves: list[str] = []  # solution nodes with no resolvable evidence


def _find_cycle(node_ids: list[str], edges: dict[str, list[str]]) -> bool:
    """Return True if the ``supported_by`` graph contains a cycle."""
    WHITE, GREY, BLACK = 0, 1, 2
    color = dict.fromkeys(node_ids, WHITE)

    def visit(n: str) -> bool:
        color[n] = GREY
        for child in edges.get(n, []):
            if color.get(child) == GREY:
                return True
            if color.get(child) == WHITE and visit(child):
                return True
        color[n] = BLACK
        return False

    return any(color[n] == WHITE and visit(n) for n in node_ids)


def _reachable_from(start: str, edges: dict[str, list[str]]) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        stack.extend(edges.get(n, []))
    return seen


def validate_case(
    case: AssuranceCase,
    resolvable_digests: frozenset[str] | set[str] = frozenset(),
) -> CaseValidation:
    """Validate an assurance case's structure and resolve its evidence leaves.

    ``resolvable_digests`` is the set of capsule-root digests known to resolve
    offline; a ``solution`` node counts as supported only if at least one of its
    evidence refs is in that set.
    """
    errors: list[str] = []
    ids = [n.id for n in case.nodes]

    # Duplicate ids — checked first; downstream logic assumes unique ids.
    seen: set[str] = set()
    dups: set[str] = set()
    for nid in ids:
        (dups if nid in seen else seen).add(nid)
    for d in sorted(dups):
        errors.append(f"duplicate node id: {d!r}")

    by_id = {n.id: n for n in case.nodes}
    edges = {n.id: list(n.supported_by) for n in case.nodes}

    # Unknown supported_by references.
    for n in case.nodes:
        for child in n.supported_by:
            if child not in by_id:
                errors.append(f"node {n.id!r} references unknown node {child!r}")

    # Top goal: a `goal` node that no node supports.
    supported_ids = {c for children in edges.values() for c in children}
    top_goals = [n.id for n in case.nodes if n.type == NodeType.goal and n.id not in supported_ids]
    top_goal_id: str | None = None
    if len(top_goals) == 1:
        top_goal_id = top_goals[0]
    elif len(top_goals) == 0:
        errors.append("no top goal: exactly one unsupported `goal` node is required")
    else:
        errors.append(f"multiple top goals: {sorted(top_goals)}")

    # Cycle detection (only meaningful once refs are known; still safe otherwise).
    if _find_cycle(list(by_id), edges):
        errors.append("cycle detected in the supported_by graph")

    # Orphans: nodes unreachable from the top goal (only when the graph is acyclic
    # and has a single top goal — otherwise the earlier error is the real cause).
    if top_goal_id is not None and not any("cycle" in e for e in errors):
        reachable = _reachable_from(top_goal_id, edges)
        orphans = sorted(nid for nid in by_id if nid not in reachable)
        if orphans:
            errors.append(f"orphan node(s) not reachable from top goal: {orphans}")

    # Unsupported leaves: solution nodes with no resolvable evidence (non-fatal).
    resolvable = set(resolvable_digests)
    unsupported = sorted(
        n.id
        for n in case.nodes
        if n.type == NodeType.solution
        and not any(ref.digest in resolvable for ref in n.evidence_refs)
    )

    return CaseValidation(
        valid=not errors,
        errors=errors,
        top_goal_id=top_goal_id,
        unsupported_leaves=unsupported,
    )
