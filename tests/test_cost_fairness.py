"""ADR-0146 D5 (first slice) — agent cost/energy fairness ledger (NF-150).

Reports each agent's **relative share** of a resource (cost / energy / calls) as a normalized
descriptive statistic — share, Gini coefficient, max/mean ratio. It is **read-only evidence**, a
descriptive statistic and never a threshold, quota, or pass/fail verdict.
"""
from __future__ import annotations

import math

from novafabric.cost.fairness import (
    FairnessMetric,
    FairnessReport,
    build_fairness_report,
    compute_fairness,
)


def test_equal_distribution_has_zero_gini():
    m = compute_fairness({"a": 25.0, "b": 25.0, "c": 25.0, "d": 25.0}, dimension="cost")
    assert isinstance(m, FairnessMetric)
    assert m.dimension == "cost"
    assert m.gini == 0.0
    assert m.max_mean_ratio == 1.0
    assert all(abs(s - 0.25) < 1e-9 for s in m.shares.values())


def test_full_concentration_gini_is_n_minus_1_over_n():
    m = compute_fairness({"a": 100.0, "b": 0.0, "c": 0.0, "d": 0.0}, dimension="cost")
    assert math.isclose(m.gini, 0.75, abs_tol=1e-9)   # (n-1)/n for total concentration
    assert math.isclose(m.max_mean_ratio, 4.0, abs_tol=1e-9)  # == n
    assert m.shares["a"] == 1.0


def test_shares_sum_to_one():
    m = compute_fairness({"a": 3.0, "b": 1.0}, dimension="calls")
    assert math.isclose(sum(m.shares.values()), 1.0, abs_tol=1e-9)


def test_no_verdict_or_threshold_field():
    # descriptive evidence only — never a quota / pass-fail
    for forbidden in ("verdict", "threshold", "quota", "pass", "over_limit"):
        assert forbidden not in FairnessMetric.model_fields


def test_single_agent_is_perfectly_equal():
    m = compute_fairness({"solo": 42.0}, dimension="energy")
    assert m.gini == 0.0
    assert m.max_mean_ratio == 1.0
    assert m.shares == {"solo": 1.0}


def test_empty_totals_are_safe():
    m = compute_fairness({}, dimension="cost")
    assert m.shares == {}
    assert m.gini == 0.0
    assert m.max_mean_ratio == 0.0


def test_shares_are_deterministically_ordered():
    m = compute_fairness({"z": 1.0, "a": 1.0, "m": 1.0}, dimension="cost")
    assert list(m.shares.keys()) == ["a", "m", "z"]


def test_report_covers_each_dimension_sorted():
    report = build_fairness_report({
        "energy": {"a": 1.0, "b": 1.0},
        "cost": {"a": 2.0, "b": 0.0},
    })
    assert isinstance(report, FairnessReport)
    assert [m.dimension for m in report.metrics] == ["cost", "energy"]  # sorted
