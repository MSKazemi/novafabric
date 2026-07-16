"""Tests for the offline silent-failure detector (ADR-0147 D6 / NF-158)."""
from __future__ import annotations

import pytest

from novafabric.drift.silent_failure import (
    SilentFailureRecord,
    detect_silent_failures,
)


def _run(run_id: str, status: str, q: float) -> dict[str, object]:
    return {"run_id": run_id, "status": status, "quality_signal": q}


def test_success_with_low_quality_is_flagged() -> None:
    report = detect_silent_failures([_run("r1", "success", 0.2)], threshold=0.5)
    rec = report.records[0]
    assert isinstance(rec, SilentFailureRecord)
    assert rec.silent_failure is True


def test_success_with_high_quality_is_not_flagged() -> None:
    report = detect_silent_failures([_run("r1", "success", 0.9)], threshold=0.5)
    assert report.records[0].silent_failure is False


def test_threshold_boundary_is_not_below() -> None:
    # quality == threshold is not "below" — not a silent failure.
    report = detect_silent_failures([_run("r1", "success", 0.5)], threshold=0.5)
    assert report.records[0].silent_failure is False


def test_reported_failure_is_never_silent() -> None:
    # A run that already reported failure is not a *silent* failure, however low its quality.
    report = detect_silent_failures([_run("r1", "failure", 0.01)], threshold=0.5)
    assert report.records[0].silent_failure is False


def test_custom_success_statuses() -> None:
    # An operator may treat 'partial' as a reported-ok terminal state.
    report = detect_silent_failures(
        [_run("r1", "partial", 0.2)], threshold=0.5, success_statuses=("success", "partial"),
    )
    assert report.records[0].silent_failure is True


def test_summary_counts() -> None:
    runs = [
        _run("r1", "success", 0.2),   # flagged
        _run("r2", "success", 0.9),   # ok
        _run("r3", "failure", 0.1),   # not silent (reported failure)
        _run("r4", "success", 0.1),   # flagged
    ]
    report = detect_silent_failures(runs, threshold=0.5)
    assert report.total_reported_success == 3
    assert report.silent_failures == 2


def test_empty_runs() -> None:
    report = detect_silent_failures([], threshold=0.5)
    assert report.records == []
    assert report.silent_failures == 0


def test_no_verdict_field_beyond_the_flag() -> None:
    report = detect_silent_failures([_run("r1", "success", 0.2)], threshold=0.5)
    rec_keys = report.records[0].model_dump().keys()
    forbidden = {"failed", "passed", "pass", "quality_ok", "verdict", "ok", "certified"}
    assert forbidden.isdisjoint(rec_keys)
    assert forbidden.isdisjoint(report.model_dump().keys())


def test_missing_quality_signal_rejected() -> None:
    with pytest.raises(ValueError):
        detect_silent_failures([{"run_id": "r1", "status": "success"}], threshold=0.5)
