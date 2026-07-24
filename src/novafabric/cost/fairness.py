"""Agent cost/energy fairness ledger (ADR-0146 D5 / NF-150, first slice).

Reports each agent's **relative share** of a resource (cost / energy / calls) as a normalized
descriptive statistic — share, Gini coefficient, and max/mean ratio. Per ADR-0146 D5 this is
**read-only evidence**: a descriptive statistic, never a threshold, quota, or pass/fail verdict
(:class:`FairnessMetric` carries no such field).

This first slice is the pure statistic over supplied per-agent totals. The collector that derives
those totals from the records a capsule already holds (``cost/`` interceptor + ``energy/``
attribution — "no new capture primitive is introduced", ADR-0146 D1) is a documented follow-on.
"""
from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel


class FairnessMetric(BaseModel):
    dimension: str  # "cost" | "energy" | "calls"
    shares: dict[str, float]  # agent -> normalized share (sums to 1.0), keys sorted
    gini: float  # 0.0 = perfectly equal … → (n-1)/n at full concentration
    max_mean_ratio: float  # max / mean (1.0 = perfectly equal; == n at full concentration)
    # Intentionally NO verdict/threshold/quota field — descriptive evidence, never a decision.


class FairnessReport(BaseModel):
    metrics: list[FairnessMetric]


def _gini(values: list[float], total: float) -> float:
    n = len(values)
    if n == 0 or total <= 0:
        return 0.0
    abs_diff_sum = sum(abs(a - b) for a in values for b in values)
    return abs_diff_sum / (2 * n * total)


def compute_fairness(totals: Mapping[str, float], *, dimension: str) -> FairnessMetric:
    """Compute the fairness statistic for one resource ``dimension`` over per-agent ``totals``.

    ``totals`` maps each agent to its non-negative resource total. Shares are normalized and the
    keys are sorted for deterministic, byte-identical output. An empty or all-zero input yields a
    safe zero metric.
    """
    items = sorted(totals.items())  # deterministic order
    keys = [k for k, _ in items]
    values = [float(v) for _, v in items]
    total = sum(values)
    n = len(values)

    if n == 0 or total <= 0:
        return FairnessMetric(
            dimension=dimension,
            shares=dict.fromkeys(keys, 0.0),
            gini=0.0,
            max_mean_ratio=0.0,
        )

    shares = {k: v / total for k, v in zip(keys, values, strict=True)}
    mean = total / n
    max_mean_ratio = max(values) / mean
    return FairnessMetric(
        dimension=dimension,
        shares=shares,
        gini=_gini(values, total),
        max_mean_ratio=max_mean_ratio,
    )


def build_fairness_report(
    totals_by_dimension: Mapping[str, Mapping[str, float]],
) -> FairnessReport:
    """Build a :class:`FairnessReport` with one metric per dimension (dimensions sorted)."""
    return FairnessReport(
        metrics=[
            compute_fairness(totals, dimension=dim)
            for dim, totals in sorted(totals_by_dimension.items())
        ]
    )
