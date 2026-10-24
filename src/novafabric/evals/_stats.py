from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from novafabric.evals.result import EvalResult, StatisticalContext


class MetricComparison(BaseModel):
    """One metric in both runs.

    Every eval metric is higher-is-better: the schema defines ``threshold`` as the
    minimum acceptable value and ``passed`` as ``value >= threshold``. So
    ``significant`` means a significant **decrease** (a regression), and
    ``improved`` a significant increase; ``p_value`` is the one-sided p-value for
    a decrease when the z-test ran.
    """

    name: str
    baseline_value: float
    candidate_value: float
    delta: float
    relative_delta: float
    significant: bool
    improved: bool = False
    p_value: Optional[float] = None
    statistical_context: Optional[StatisticalContext] = None


class RegressionReport(BaseModel):
    """The comparison of two runs of one suite.

    ``comparable`` is False when the runs are of different suites or a baseline
    metric is missing from the candidate (``missing_metrics``): such a result is
    neither a pass nor a regression, it cannot be judged (``nova eval compare``
    exits 2). A metric only the candidate has is ignored.
    """

    suite_id: str
    baseline_run_at: datetime
    candidate_run_at: datetime
    metrics: list[MetricComparison]
    regression_detected: bool
    summary: str
    candidate_suite_id: str = ""
    missing_metrics: list[str] = []
    comparable: bool = True


def _std_from_ci(
    ci_low: float, ci_high: float, sample_size: int, z_critical: float = 1.96
) -> float:
    """Recover population std estimate from a 95% confidence interval.

    A 95% CI is constructed as mean ± z * (sigma / sqrt(n)).
    Therefore sigma = (ci_high - ci_low) / (2 * z) * sqrt(n).
    """
    half_width = (ci_high - ci_low) / 2.0
    return half_width / z_critical * math.sqrt(sample_size)


def _lower_tail_p_value(z_stat: float) -> float:
    """One-sided p-value for a *decrease*: P(Z <= z) = Phi(z) = erfc(-z / sqrt(2)) / 2.

    Small only when the candidate is significantly *below* the baseline; an
    improvement gives a p-value above 0.5.
    """
    return 0.5 * math.erfc(-z_stat / math.sqrt(2.0))


def _two_sample_z_stat(
    mean1: float,
    std1: float,
    n1: int,
    mean2: float,
    std2: float,
    n2: int,
) -> float:
    """Two-sample z-statistic for the difference in means (mean2 - mean1)."""
    se = math.sqrt((std1 ** 2 / n1) + (std2 ** 2 / n2))
    if se == 0.0:
        return 0.0
    return (mean2 - mean1) / se


class RegressionDetector:
    """Detect metric regressions between two :class:`~novafabric.evals.result.EvalResult` runs.

    A regression is a significant *decrease* (metrics are higher-is-better, see
    :class:`MetricComparison`). When both results carry a
    :class:`~novafabric.evals.result.StatisticalContext` with
    ``sample_size >= min_samples`` and a ``confidence_interval``, a one-sided
    z-test decides; otherwise a metric regresses when its relative decrease
    exceeds ``alpha``. An improvement is never a regression (review cli-a-02).
    """

    def __init__(self, alpha: float = 0.05, min_samples: int = 5) -> None:
        self.alpha = alpha
        self.min_samples = min_samples

    def compare(self, baseline: EvalResult, candidate: EvalResult) -> RegressionReport:
        baseline_by_name = {m.name: m for m in baseline.metrics}
        candidate_by_name = {m.name: m for m in candidate.metrics}
        common_names = sorted(set(baseline_by_name) & set(candidate_by_name))
        missing = sorted(set(baseline_by_name) - set(candidate_by_name))
        same_suite = baseline.suite_id == candidate.suite_id

        comparisons: list[MetricComparison] = []
        for name in common_names:
            bm = baseline_by_name[name]
            cm = candidate_by_name[name]

            delta = cm.value - bm.value
            if bm.value == 0.0:
                relative_delta = math.inf if delta != 0.0 else 0.0
            else:
                relative_delta = delta / bm.value

            p_value, stat_ctx, significant, improved = self._assess_significance(
                bm.value, cm.value, baseline.statistical_context, candidate.statistical_context
            )

            comparisons.append(
                MetricComparison(
                    name=name,
                    baseline_value=bm.value,
                    candidate_value=cm.value,
                    delta=delta,
                    relative_delta=relative_delta,
                    significant=significant,
                    improved=improved,
                    p_value=p_value,
                    statistical_context=stat_ctx,
                )
            )

        regression_detected = any(mc.significant for mc in comparisons)
        comparable = same_suite and not missing

        n_sig = sum(1 for mc in comparisons if mc.significant)
        n_total = len(comparisons)
        if not same_suite:
            summary = (
                f"Cannot compare: baseline suite {baseline.suite_id!r}, "
                f"candidate suite {candidate.suite_id!r}."
            )
        elif missing:
            summary = (
                f"Cannot compare: the candidate has no {', '.join(missing)} "
                f"(baseline metric{'s' if len(missing) > 1 else ''})."
            )
        elif regression_detected:
            summary = (
                f"Regression detected: {n_sig}/{n_total} metric(s) show statistically "
                f"significant degradation (alpha={self.alpha})."
            )
        else:
            summary = (
                f"No regression detected: {n_total} metric(s) compared, "
                f"none exceeded the significance threshold (alpha={self.alpha})."
            )

        return RegressionReport(
            suite_id=baseline.suite_id,
            baseline_run_at=baseline.run_at,
            candidate_run_at=candidate.run_at,
            metrics=comparisons,
            regression_detected=regression_detected,
            summary=summary,
            candidate_suite_id=candidate.suite_id,
            missing_metrics=missing,
            comparable=comparable,
        )

    def _assess_significance(
        self,
        baseline_value: float,
        candidate_value: float,
        baseline_ctx: Optional[StatisticalContext],
        candidate_ctx: Optional[StatisticalContext],
    ) -> tuple[Optional[float], Optional[StatisticalContext], bool, bool]:
        """Return (p_value, stat_ctx_for_report, regressed, improved).

        Uses the one-sided z-test when both contexts have sufficient samples and
        CIs; otherwise falls back to the relative-change threshold.
        """
        if (
            baseline_ctx is not None
            and candidate_ctx is not None
            and baseline_ctx.sample_size is not None
            and candidate_ctx.sample_size is not None
            and baseline_ctx.sample_size >= self.min_samples
            and candidate_ctx.sample_size >= self.min_samples
            and baseline_ctx.confidence_interval is not None
            and candidate_ctx.confidence_interval is not None
        ):
            b_n = baseline_ctx.sample_size
            c_n = candidate_ctx.sample_size
            b_ci = baseline_ctx.confidence_interval
            c_ci = candidate_ctx.confidence_interval

            b_std = _std_from_ci(b_ci[0], b_ci[1], b_n)
            c_std = _std_from_ci(c_ci[0], c_ci[1], c_n)

            z = _two_sample_z_stat(baseline_value, b_std, b_n, candidate_value, c_std, c_n)
            p_value = _lower_tail_p_value(z)
            regressed = p_value < self.alpha
            improved = (1.0 - p_value) < self.alpha  # the upper tail
            return p_value, candidate_ctx, regressed, improved

        # Relative-change fallback, signed: positive = the candidate is better.
        change = candidate_value - baseline_value
        if baseline_value == 0.0:
            relative_change = math.copysign(math.inf, change) if change else 0.0
        else:
            relative_change = change / abs(baseline_value)
        return None, None, relative_change < -self.alpha, relative_change > self.alpha
