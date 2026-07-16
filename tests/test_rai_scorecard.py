"""ADR-0158 D4 (first slice) — Responsible-AI scorecard (NF-262).

The RAI scorecard is **presence/coverage of evidence per cell** (``supported | partial |
unsupported | not_applicable``) — never a numeric "responsibility score" (ADR-0158 I-4).
NovaFabric applies no threshold and assigns no fair/unfair or pass/fail label; a cell only records
whether evidence for that dimension is present.
"""
from __future__ import annotations

from novafabric.compliance.rai.scorecard import (
    RAI_DIMENSIONS,
    CellCoverage,
    RAIScorecard,
    ScorecardCell,
    build_rai_scorecard,
)


def test_dimension_with_evidence_is_supported():
    sc = build_rai_scorecard({"fairness": ["capsule://run/fairness#d1"]})
    cell = next(c for c in sc.cells if c.dimension == "fairness")
    assert cell.coverage is CellCoverage.supported
    assert cell.evidence_refs == ["capsule://run/fairness#d1"]


def test_dimension_without_evidence_is_unsupported():
    sc = build_rai_scorecard({})
    cell = next(c for c in sc.cells if c.dimension == "fairness")
    assert cell.coverage is CellCoverage.unsupported
    assert cell.evidence_refs == []


def test_not_applicable_is_declared_and_wins_over_evidence():
    sc = build_rai_scorecard(
        {"accessibility": ["capsule://x"]}, not_applicable=["accessibility"]
    )
    cell = next(c for c in sc.cells if c.dimension == "accessibility")
    assert cell.coverage is CellCoverage.not_applicable


def test_partial_is_flagged():
    sc = build_rai_scorecard({"privacy": ["capsule://p"]}, partial=["privacy"])
    cell = next(c for c in sc.cells if c.dimension == "privacy")
    assert cell.coverage is CellCoverage.partial
    assert cell.evidence_refs == ["capsule://p"]


def test_cells_follow_the_fixed_dimension_order():
    sc = build_rai_scorecard({})
    assert [c.dimension for c in sc.cells] == list(RAI_DIMENSIONS)


def test_summary_counts_each_coverage_state():
    sc = build_rai_scorecard(
        {"fairness": ["a"], "privacy": ["b"]},
        not_applicable=["accessibility"],
        partial=["privacy"],
    )
    s = sc.summary
    assert s["supported"] == 1        # fairness
    assert s["partial"] == 1          # privacy
    assert s["not_applicable"] == 1   # accessibility
    assert s["unsupported"] == len(RAI_DIMENSIONS) - 3


def test_no_numeric_score_field_anywhere():
    for forbidden in ("score", "responsibility_score", "rating", "grade", "verdict"):
        assert forbidden not in RAIScorecard.model_fields
        assert forbidden not in ScorecardCell.model_fields
