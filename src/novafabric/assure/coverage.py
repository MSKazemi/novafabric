"""Structural argument coverage for an assurance case (ADR-0166 D4, NF-348) — never a grade.

Confidence has a *negative view*: an argument is as strong as its weakest unanswered leaf and its
open challenges. ``compute_argument_coverage`` reports the **structural** coverage of the in-tree D1
argument graph, cross-cut by the D4 defeaters and the D2 currency ledger:

* ``total_goals`` — how many ``goal`` nodes the argument makes,
* ``goals_with_resolvable_leaf`` — how many of them transitively reach at least one ``solution``
  leaf whose evidence resolves offline,
* ``unsupported_leaves`` — the ``solution`` nodes with no resolvable evidence,
* ``open_defeaters`` — how many challenges are still unanswered,
* ``overdue_nodes`` — the nodes whose evidence window has expired at a *supplied* sealed time.

Per the ADR it reports coverage **and never a pass/fail grade or a numeric "assurance score"** that
could be read as a verdict: there is deliberately no grade/score/pass field. It is a pure read over
already-modelled facts — no capsule mutation, no capture-path change, and (honouring D2) currency is
only ever computed against an explicit ``as_of`` sealed time, never the system clock.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from novafabric.assure.case import AssuranceCase, NodeType, validate_case
from novafabric.assure.currency import CurrencyLedger, IntervalStatus, compute_interval_status
from novafabric.assure.defeater import Defeater, open_defeaters


class ArgumentCoverage(BaseModel):
    """Structural coverage of an assurance-case argument — counts and gaps, never a grade.

    Intentionally carries **no** grade / score / pass / verdict field: it reports what is covered
    and what is open, and a qualified human draws the sufficiency conclusion.
    """

    total_goals: int
    goals_with_resolvable_leaf: int
    unsupported_leaves: list[str]
    open_defeaters: int
    overdue_nodes: list[str]


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


def compute_argument_coverage(
    case: AssuranceCase,
    *,
    resolvable_digests: frozenset[str] | set[str] = frozenset(),
    defeaters: list[Defeater] | tuple[Defeater, ...] = (),
    ledger: CurrencyLedger | None = None,
    as_of: datetime | None = None,
) -> ArgumentCoverage:
    """Compute structural coverage of ``case`` — counts and gaps, never a grade.

    ``resolvable_digests`` are the capsule-root digests known to resolve offline (a ``solution``
    leaf counts as supported only if one of its evidence refs is in that set). ``defeaters``
    supplies the D4 challenges; only ``open`` ones are counted. ``ledger`` supplies the D2 currency
    entries; when it carries entries an explicit ``as_of`` sealed time is **required** — currency is
    never computed against the system clock (ADR-0166 D2).
    """
    resolvable = set(resolvable_digests)
    by_id = {n.id: n for n in case.nodes}
    edges = {n.id: list(n.supported_by) for n in case.nodes}

    # A solution leaf is "resolvable" when at least one of its evidence refs resolves offline.
    resolvable_solutions = {
        n.id
        for n in case.nodes
        if n.type is NodeType.solution
        and any(ref.digest in resolvable for ref in n.evidence_refs)
    }

    goal_ids = [n.id for n in case.nodes if n.type is NodeType.goal]
    goals_with_leaf = sum(
        1
        for gid in goal_ids
        if _reachable_from(gid, edges) & resolvable_solutions
    )

    # Unsupported leaves — reuse the D1 validator's definition so the two never drift apart.
    unsupported = validate_case(case, resolvable).unsupported_leaves

    overdue: list[str] = []
    if ledger is not None and ledger.nodes:
        if as_of is None:
            raise ValueError(
                "as_of (a sealed time) is required to compute currency — never the system clock"
            )
        overdue = sorted(
            n.node_id
            for n in ledger.nodes
            if n.node_id in by_id
            and compute_interval_status(n, as_of=as_of) is IntervalStatus.overdue
        )

    return ArgumentCoverage(
        total_goals=len(goal_ids),
        goals_with_resolvable_leaf=goals_with_leaf,
        unsupported_leaves=unsupported,
        open_defeaters=len(open_defeaters(list(defeaters))),
        overdue_nodes=overdue,
    )
