"""Tests for regression_report field in PolicyResource and regression gate integration."""
from __future__ import annotations

from datetime import datetime, timezone

from novafabric.evals._stats import MetricComparison, RegressionReport
from novafabric.policy import PolicyInput, PolicyResource, PolicySubject


def _make_regression_report(regression_detected: bool) -> RegressionReport:
    return RegressionReport(
        suite_id="gaia-v1",
        baseline_run_at=datetime(2026, 5, 10, 0, 0, 0, tzinfo=timezone.utc),
        candidate_run_at=datetime(2026, 5, 11, 0, 0, 0, tzinfo=timezone.utc),
        metrics=[
            MetricComparison(
                name="accuracy",
                baseline_value=0.85,
                candidate_value=0.80 if regression_detected else 0.87,
                delta=-0.05 if regression_detected else 0.02,
                relative_delta=-0.059 if regression_detected else 0.024,
                significant=regression_detected,
                p_value=0.03 if regression_detected else 0.42,
            )
        ],
        regression_detected=regression_detected,
        summary="Regression detected: accuracy dropped" if regression_detected else "No regression",
    )


def test_policy_resource_accepts_regression_report() -> None:
    """PolicyResource accepts a serialized RegressionReport in regression_report field."""
    report = _make_regression_report(regression_detected=False)
    resource = PolicyResource(
        kind="capsule",
        ref="capsule-001",
        regression_report=report.model_dump(mode="json"),
    )
    assert resource.regression_report is not None
    assert resource.regression_report["regression_detected"] is False


def test_policy_resource_regression_report_defaults_to_none() -> None:
    """PolicyResource.regression_report is None when not provided."""
    resource = PolicyResource(kind="asset", ref="asset-001")
    assert resource.regression_report is None


def test_policy_input_roundtrip_with_regression_report() -> None:
    """PolicyInput with regression_report serialises and re-validates."""
    report = _make_regression_report(regression_detected=True)
    inp = PolicyInput(
        action="promote",
        subject=PolicySubject(user="alice", roles=["admin"]),
        resource=PolicyResource(
            kind="capsule",
            ref="capsule-002",
            regression_report=report.model_dump(mode="json"),
        ),
    )
    raw = inp.model_dump_json()
    restored = PolicyInput.model_validate_json(raw)
    assert restored.resource.regression_report is not None
    assert restored.resource.regression_report["regression_detected"] is True
    assert restored.resource.regression_report["suite_id"] == "gaia-v1"


def test_regression_report_serialization_preserves_all_fields() -> None:
    """Serialized RegressionReport dict contains all fields the Rego gate expects."""
    report = _make_regression_report(regression_detected=True)
    serialized = report.model_dump(mode="json")
    assert "regression_detected" in serialized
    assert "suite_id" in serialized
    assert "metrics" in serialized
    assert len(serialized["metrics"]) == 1
