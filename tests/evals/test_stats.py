from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from novafabric.evals._stats import RegressionDetector, RegressionReport
from novafabric.evals.result import EvalResult, Metric, StatisticalContext

# ── helpers ───────────────────────────────────────────────────────────────────

def _make_result(
    suite_id: str = "test-suite-v1",
    metrics: list[Metric] | None = None,
    statistical_context: StatisticalContext | None = None,
    run_at: datetime | None = None,
) -> EvalResult:
    if metrics is None:
        metrics = [Metric(name="accuracy", value=0.90, unit="score", threshold=0.80, passed=True)]
    return EvalResult(
        suite_id=suite_id,
        suite_version="0.1.0",
        oci_digest="",
        run_at=run_at or datetime(2026, 1, 1, tzinfo=timezone.utc),
        capsule_id="cap-001",
        passed=True,
        metrics=metrics,
        statistical_context=statistical_context,
    )


def _make_metric(name: str = "accuracy", value: float = 0.90) -> Metric:
    return Metric(name=name, value=value, unit="score", threshold=0.80, passed=value >= 0.80)


# ── RegressionDetector.compare — identical results → no regression ────────────

def test_compare_identical_results_no_regression() -> None:
    result = _make_result()
    detector = RegressionDetector(alpha=0.05, min_samples=5)
    report = detector.compare(baseline=result, candidate=result)
    assert report.regression_detected is False
    assert len(report.metrics) == 1
    mc = report.metrics[0]
    assert mc.delta == pytest.approx(0.0)
    assert mc.relative_delta == pytest.approx(0.0)
    assert mc.significant is False


# ── large delta above alpha threshold → regression detected (delta fallback) ──

def test_compare_large_delta_regression_detected() -> None:
    baseline = _make_result(
        metrics=[_make_metric("accuracy", 0.90)]
    )
    candidate = _make_result(
        metrics=[_make_metric("accuracy", 0.50)]
    )
    detector = RegressionDetector(alpha=0.05, min_samples=5)
    report = detector.compare(baseline=baseline, candidate=candidate)
    assert report.regression_detected is True
    mc = report.metrics[0]
    assert mc.delta == pytest.approx(0.50 - 0.90)
    assert mc.significant is True


# ── small delta below alpha threshold → no regression (delta fallback) ────────

def test_compare_small_delta_no_regression() -> None:
    baseline = _make_result(metrics=[_make_metric("accuracy", 0.90)])
    candidate = _make_result(metrics=[_make_metric("accuracy", 0.902)])
    detector = RegressionDetector(alpha=0.05, min_samples=5)
    report = detector.compare(baseline=baseline, candidate=candidate)
    assert report.regression_detected is False
    mc = report.metrics[0]
    assert mc.significant is False


# ── StatisticalContext with sample_size >= min_samples → z-test path ──────────

def test_compare_uses_ztest_with_statistical_context() -> None:
    """Two results with identical mean values (via CI) — no regression."""
    ctx = StatisticalContext(sample_size=30, confidence_interval=[0.88, 0.92])
    baseline = _make_result(
        metrics=[_make_metric("accuracy", 0.90)],
        statistical_context=ctx,
    )
    candidate = _make_result(
        metrics=[_make_metric("accuracy", 0.901)],
        statistical_context=ctx,
    )
    detector = RegressionDetector(alpha=0.05, min_samples=5)
    report = detector.compare(baseline=baseline, candidate=candidate)
    # Very tiny delta on large sample → p_value should be large, not significant
    mc = report.metrics[0]
    assert mc.p_value is not None, "z-test path should populate p_value"
    assert mc.significant is False
    assert mc.statistical_context is not None


def test_compare_ztest_clearly_significant_delta() -> None:
    """Large delta on tight CI → z-test should flag as significant."""
    # CI of [0.88, 0.92] with n=100 means tight std; big delta is significant
    ctx_baseline = StatisticalContext(sample_size=100, confidence_interval=[0.88, 0.92])
    ctx_candidate = StatisticalContext(sample_size=100, confidence_interval=[0.58, 0.62])
    baseline = _make_result(
        metrics=[_make_metric("accuracy", 0.90)],
        statistical_context=ctx_baseline,
    )
    candidate = _make_result(
        metrics=[_make_metric("accuracy", 0.60)],
        statistical_context=ctx_candidate,
    )
    detector = RegressionDetector(alpha=0.05, min_samples=5)
    report = detector.compare(baseline=baseline, candidate=candidate)
    mc = report.metrics[0]
    assert mc.p_value is not None
    assert mc.p_value < 0.05, f"Expected p_value < 0.05, got {mc.p_value}"
    assert mc.significant is True
    assert report.regression_detected is True


# ── insufficient samples → delta fallback, p_value is None ───────────────────

def test_compare_insufficient_samples_falls_back_to_delta() -> None:
    ctx = StatisticalContext(sample_size=2, confidence_interval=[0.85, 0.95])
    baseline = _make_result(
        metrics=[_make_metric("accuracy", 0.90)],
        statistical_context=ctx,
    )
    candidate = _make_result(
        metrics=[_make_metric("accuracy", 0.85)],
        statistical_context=ctx,
    )
    detector = RegressionDetector(alpha=0.05, min_samples=5)
    report = detector.compare(baseline=baseline, candidate=candidate)
    mc = report.metrics[0]
    # sample_size=2 < min_samples=5 → no z-test
    assert mc.p_value is None, "Should not compute z-test with insufficient samples"
    # delta = -0.05 / 0.90 ≈ -0.0556 → |rel_delta| > 0.05 → significant
    assert mc.significant is True


# ── RegressionReport fields are populated correctly ───────────────────────────

def test_regression_report_fields_populated() -> None:
    baseline_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candidate_time = datetime(2026, 1, 2, tzinfo=timezone.utc)
    baseline = _make_result(
        suite_id="my-suite-v1",
        metrics=[_make_metric("accuracy", 0.90), _make_metric("latency_p99", 0.20)],
        run_at=baseline_time,
    )
    candidate = _make_result(
        suite_id="my-suite-v1",
        metrics=[_make_metric("accuracy", 0.88), _make_metric("latency_p99", 0.22)],
        run_at=candidate_time,
    )
    detector = RegressionDetector(alpha=0.05, min_samples=5)
    report = detector.compare(baseline=baseline, candidate=candidate)

    assert isinstance(report, RegressionReport)
    assert report.suite_id == "my-suite-v1"
    assert report.baseline_run_at == baseline_time
    assert report.candidate_run_at == candidate_time
    assert len(report.metrics) == 2
    assert isinstance(report.summary, str) and len(report.summary) > 0

    names = {mc.name for mc in report.metrics}
    assert names == {"accuracy", "latency_p99"}

    acc = next(mc for mc in report.metrics if mc.name == "accuracy")
    assert acc.baseline_value == pytest.approx(0.90)
    assert acc.candidate_value == pytest.approx(0.88)
    assert acc.delta == pytest.approx(0.88 - 0.90)


# ── relative_delta with baseline == 0 → inf ───────────────────────────────────

def test_relative_delta_zero_baseline_is_inf() -> None:
    baseline = _make_result(
        metrics=[Metric(name="x", value=0.0, unit="s", threshold=0.0, passed=True)]
    )
    candidate = _make_result(
        metrics=[Metric(name="x", value=1.0, unit="s", threshold=0.0, passed=True)]
    )
    detector = RegressionDetector()
    report = detector.compare(baseline=baseline, candidate=candidate)
    mc = report.metrics[0]
    assert math.isinf(mc.relative_delta)


# ── metric name mismatch is handled gracefully ────────────────────────────────

def test_compare_metric_name_mismatch_skips_unmatched() -> None:
    baseline = _make_result(metrics=[_make_metric("accuracy", 0.90)])
    candidate = _make_result(metrics=[_make_metric("f1_score", 0.88)])
    detector = RegressionDetector()
    report = detector.compare(baseline=baseline, candidate=candidate)
    # No metrics in common → nothing compared, and the baseline's metric is missing:
    # the result cannot be called a pass (review cli-a-02).
    assert len(report.metrics) == 0
    assert report.regression_detected is False
    assert report.missing_metrics == ["accuracy"]
    assert report.comparable is False


# ── z-test p_value is < alpha for clearly significant delta ───────────────────

def test_ztest_pvalue_below_alpha_for_large_delta() -> None:
    ctx_b = StatisticalContext(sample_size=50, confidence_interval=[0.89, 0.91])
    ctx_c = StatisticalContext(sample_size=50, confidence_interval=[0.49, 0.51])
    baseline = _make_result(
        metrics=[_make_metric("accuracy", 0.90)],
        statistical_context=ctx_b,
    )
    candidate = _make_result(
        metrics=[_make_metric("accuracy", 0.50)],
        statistical_context=ctx_c,
    )
    detector = RegressionDetector(alpha=0.05, min_samples=5)
    report = detector.compare(baseline=baseline, candidate=candidate)
    mc = report.metrics[0]
    assert mc.p_value is not None
    assert mc.p_value < 0.001  # should be astronomically small


# ── z-test with zero standard error (identical CI, identical value) ───────────

def test_ztest_zero_se_returns_not_significant() -> None:
    """When both CIs are point intervals (width=0), se=0 → z=0 → p=1 (not significant)."""
    ctx = StatisticalContext(sample_size=30, confidence_interval=[0.90, 0.90])
    baseline = _make_result(
        metrics=[_make_metric("accuracy", 0.90)],
        statistical_context=ctx,
    )
    candidate = _make_result(
        metrics=[_make_metric("accuracy", 0.90)],
        statistical_context=ctx,
    )
    detector = RegressionDetector(alpha=0.05, min_samples=5)
    report = detector.compare(baseline=baseline, candidate=candidate)
    mc = report.metrics[0]
    assert mc.p_value is not None
    assert mc.significant is False


# ── review cli-a-02: one-sided — only a decrease is a regression ──────────────

def test_improvement_beyond_alpha_is_not_a_regression() -> None:
    baseline = _make_result(metrics=[_make_metric("accuracy", 0.80)])
    candidate = _make_result(metrics=[_make_metric("accuracy", 0.95)])
    report = RegressionDetector(alpha=0.05).compare(baseline=baseline, candidate=candidate)
    assert report.regression_detected is False
    assert report.comparable is True
    (mc,) = report.metrics
    assert mc.significant is False and mc.improved is True


def test_ztest_significant_improvement_is_not_a_regression() -> None:
    ctx_b = StatisticalContext(sample_size=50, confidence_interval=[0.49, 0.51])
    ctx_c = StatisticalContext(sample_size=50, confidence_interval=[0.89, 0.91])
    baseline = _make_result(metrics=[_make_metric("accuracy", 0.50)], statistical_context=ctx_b)
    candidate = _make_result(metrics=[_make_metric("accuracy", 0.90)], statistical_context=ctx_c)
    report = RegressionDetector(alpha=0.05, min_samples=5).compare(
        baseline=baseline, candidate=candidate
    )
    assert report.regression_detected is False
    (mc,) = report.metrics
    assert mc.improved is True
    assert mc.p_value is not None and mc.p_value > 0.5  # one-sided p for a decrease


def test_ztest_one_sided_p_value_for_a_decrease_is_half_the_two_sided() -> None:
    ctx_b = StatisticalContext(sample_size=50, confidence_interval=[0.80, 0.90])
    ctx_c = StatisticalContext(sample_size=50, confidence_interval=[0.75, 0.85])
    baseline = _make_result(metrics=[_make_metric("accuracy", 0.85)], statistical_context=ctx_b)
    candidate = _make_result(metrics=[_make_metric("accuracy", 0.80)], statistical_context=ctx_c)
    (mc,) = RegressionDetector(alpha=0.05, min_samples=5).compare(
        baseline=baseline, candidate=candidate
    ).metrics
    se = math.sqrt(2 * ((0.05 / 1.96 * math.sqrt(50)) ** 2 / 50))
    z = (0.80 - 0.85) / se
    assert mc.p_value == pytest.approx(0.5 * math.erfc(-z / math.sqrt(2)))


def test_suite_mismatch_is_not_comparable() -> None:
    baseline = _make_result(suite_id="a")
    candidate = _make_result(suite_id="b")
    report = RegressionDetector().compare(baseline=baseline, candidate=candidate)
    assert report.comparable is False
    assert report.candidate_suite_id == "b"


def test_metric_added_by_the_candidate_is_ignored() -> None:
    baseline = _make_result(metrics=[_make_metric("accuracy", 0.90)])
    candidate = _make_result(metrics=[_make_metric("accuracy", 0.90), _make_metric("f1", 0.7)])
    report = RegressionDetector().compare(baseline=baseline, candidate=candidate)
    assert report.comparable is True and report.missing_metrics == []
