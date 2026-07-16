"""Tests for wasted/failure-spend attribution (ADR-0146 D3 / NF-148)."""
from __future__ import annotations

import pytest

from novafabric.cost.spend_attribution import (
    SpendAttribution,
    attribute_spend,
)


def _run(run_id: str, status: str, cost: float) -> dict[str, object]:
    return {"run_id": run_id, "status": status, "cost": cost}


def test_splits_productive_and_wasted() -> None:
    runs = [
        _run("r1", "success", 1.0),
        _run("r2", "failure", 0.5),
        _run("r3", "aborted", 0.25),
        _run("r4", "success", 2.0),
    ]
    report = attribute_spend(runs)
    assert isinstance(report, SpendAttribution)
    assert report.total_spend == pytest.approx(3.75)
    assert report.productive_spend == pytest.approx(3.0)
    assert report.wasted_spend == pytest.approx(0.75)
    assert report.wasted_fraction == pytest.approx(0.75 / 3.75)


def test_by_status_breakdown() -> None:
    runs = [_run("r1", "failure", 0.5), _run("r2", "failure", 0.5), _run("r3", "success", 1.0)]
    report = attribute_spend(runs)
    assert report.by_status["failure"] == pytest.approx(1.0)
    assert report.by_status["success"] == pytest.approx(1.0)


def test_all_success_zero_waste() -> None:
    report = attribute_spend([_run("r1", "success", 1.0), _run("r2", "success", 2.0)])
    assert report.wasted_spend == 0.0
    assert report.wasted_fraction == 0.0


def test_custom_productive_statuses() -> None:
    # An operator may count 'partial' as productive (partial output still delivered value).
    report = attribute_spend(
        [_run("r1", "partial", 1.0), _run("r2", "failure", 1.0)],
        productive_statuses=("success", "partial"),
    )
    assert report.productive_spend == pytest.approx(1.0)
    assert report.wasted_spend == pytest.approx(1.0)


def test_empty_runs_is_safe_zero() -> None:
    report = attribute_spend([])
    assert report.total_spend == 0.0
    assert report.wasted_fraction == 0.0
    assert report.by_status == {}


def test_no_verdict_field() -> None:
    report = attribute_spend([_run("r1", "failure", 1.0)])
    forbidden = {"verdict", "threshold", "quota", "over_budget", "acceptable", "pass", "wasteful"}
    assert forbidden.isdisjoint(report.model_dump().keys())


def test_negative_cost_rejected() -> None:
    with pytest.raises(ValueError):
        attribute_spend([_run("r1", "success", -1.0)])


def test_missing_cost_rejected() -> None:
    with pytest.raises(ValueError):
        attribute_spend([{"run_id": "r1", "status": "success"}])
