"""ADR-0166 D4 — structural argument coverage (NF-348), never a grade.

``compute_argument_coverage`` reports *structural* coverage of an assurance-case argument graph —
``total_goals``, ``goals_with_resolvable_leaf``, ``unsupported_leaves``, ``open_defeaters``,
``overdue_nodes`` — over the in-tree D1 graph, D4 defeaters, and D2 currency ledger. Per the ADR it
**never** emits a pass/fail grade or a numeric "assurance score" that could read as a verdict.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from novafabric.assure.case import (
    AssuranceCase,
    AssuranceNode,
    EvidenceRef,
    NodeType,
)
from novafabric.assure.coverage import ArgumentCoverage, compute_argument_coverage
from novafabric.assure.currency import CurrencyLedger, NodeCurrency
from novafabric.assure.defeater import Defeater, DefeaterState


def _goal(nid: str, children: list[str]) -> AssuranceNode:
    return AssuranceNode(id=nid, type=NodeType.goal, statement="g", supported_by=children)


def _solution(nid: str, digest: str) -> AssuranceNode:
    return AssuranceNode(
        id=nid,
        type=NodeType.solution,
        statement="s",
        evidence_refs=[EvidenceRef(ref="capsule://x", digest=digest)],
    )


def test_empty_case_is_all_zero():
    cov = compute_argument_coverage(AssuranceCase(case_id="c"))
    assert isinstance(cov, ArgumentCoverage)
    assert cov.total_goals == 0
    assert cov.goals_with_resolvable_leaf == 0
    assert cov.unsupported_leaves == []
    assert cov.open_defeaters == 0
    assert cov.overdue_nodes == []


def test_goal_with_resolvable_leaf_is_covered():
    case = AssuranceCase(case_id="c", nodes=[_goal("G", ["S"]), _solution("S", "d1")])
    cov = compute_argument_coverage(case, resolvable_digests={"d1"})
    assert cov.total_goals == 1
    assert cov.goals_with_resolvable_leaf == 1
    assert cov.unsupported_leaves == []


def test_goal_with_unresolvable_leaf_is_uncovered_and_flagged():
    case = AssuranceCase(case_id="c", nodes=[_goal("G", ["S"]), _solution("S", "d1")])
    cov = compute_argument_coverage(case, resolvable_digests=set())  # d1 does not resolve
    assert cov.total_goals == 1
    assert cov.goals_with_resolvable_leaf == 0
    assert cov.unsupported_leaves == ["S"]


def test_only_open_defeaters_are_counted():
    case = AssuranceCase(case_id="c", nodes=[_goal("G", ["S"]), _solution("S", "d1")])
    defeaters = [
        Defeater(id="d-open", target_node_id="G", statement="x"),
        Defeater(id="d-with", target_node_id="G", statement="x", state=DefeaterState.withdrawn),
        Defeater(
            id="d-reb", target_node_id="S", statement="x",
            state=DefeaterState.rebutted, resolved_by="capsule://r#d",
        ),
    ]
    cov = compute_argument_coverage(case, resolvable_digests={"d1"}, defeaters=defeaters)
    assert cov.open_defeaters == 1


def test_overdue_nodes_from_ledger_at_as_of():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ledger = CurrencyLedger(nodes=[
        NodeCurrency(node_id="S", last_refreshed=t0, evidence_window=timedelta(days=30)),
        NodeCurrency(node_id="G", last_refreshed=t0, evidence_window=timedelta(days=365)),
    ])
    case = AssuranceCase(case_id="c", nodes=[_goal("G", ["S"]), _solution("S", "d1")])
    as_of = t0 + timedelta(days=60)  # S overdue (30d), G still current (365d)
    cov = compute_argument_coverage(
        case, resolvable_digests={"d1"}, ledger=ledger, as_of=as_of
    )
    assert cov.overdue_nodes == ["S"]


def test_ledger_without_as_of_is_rejected():
    ledger = CurrencyLedger(nodes=[
        NodeCurrency(
            node_id="S",
            last_refreshed=datetime(2026, 1, 1, tzinfo=timezone.utc),
            evidence_window=timedelta(days=30),
        ),
    ])
    case = AssuranceCase(case_id="c", nodes=[_goal("G", ["S"]), _solution("S", "d1")])
    with pytest.raises(ValueError):
        compute_argument_coverage(case, ledger=ledger)  # no as_of — never the system clock (D2)


def test_no_grade_or_score_field():
    for forbidden in ("grade", "score", "pass", "passed", "verdict", "assurance_score", "rating"):
        assert forbidden not in ArgumentCoverage.model_fields


def test_multiple_goals_partial_coverage():
    case = AssuranceCase(
        case_id="c",
        nodes=[
            _goal("TOP", ["G1", "G2"]),
            _goal("G1", ["S1"]),
            _goal("G2", ["S2"]),
            _solution("S1", "d1"),  # resolves
            _solution("S2", "d2"),  # does not resolve
        ],
    )
    cov = compute_argument_coverage(case, resolvable_digests={"d1"})
    assert cov.total_goals == 3  # TOP, G1, G2
    # TOP reaches S1 (resolves) → covered; G1 reaches S1 → covered; G2 reaches only S2 → not.
    assert cov.goals_with_resolvable_leaf == 2
    assert cov.unsupported_leaves == ["S2"]
