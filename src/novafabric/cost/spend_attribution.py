"""Wasted / failure-spend attribution over captured run cost (ADR-0146 D3 / NF-148).

Splits the cost a set of runs already recorded into **productive** spend (runs that reported a
productive terminal status) and **wasted** spend (everything else — failed, aborted, or otherwise
non-productive trajectories), plus a per-status breakdown. Per ADR-0146 D3 this **attributes, never
re-captures**: it is descriptive arithmetic over the USD each capsule already holds, not a judgement
that any spend was unjustified. There is deliberately no verdict/threshold/quota/over_budget field —
NovaFabric attributes spend to outcomes; whether that spend was acceptable is the operator's call.

Quality note: the *tenant* (NF-149) and per-attempt *retry* (NF-147) splits are gated —
``tenant_id`` lives in the metadata store (registry-schema/infra) and per-attempt retry identifiers
are not a tested capsule facet — so only the status×cost split is shipped here.

This first slice runs over supplied ``{run_id, status, cost}`` records; the collector that derives
the per-run cost + status from the records a capsule already holds (the ``cost`` interceptor) is a
documented follow-on — no capsule mutation, no capture-path change.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import BaseModel

#: Terminal statuses treated as productive spend unless the caller overrides.
DEFAULT_PRODUCTIVE_STATUSES: tuple[str, ...] = ("success",)


class SpendAttribution(BaseModel):
    total_spend: float
    productive_spend: float
    wasted_spend: float
    wasted_fraction: float  # wasted / total, in [0, 1]; 0.0 when there is no spend
    productive_statuses: list[str]
    by_status: dict[str, float]  # status -> summed spend, keys sorted
    # Intentionally NO verdict/threshold/quota/over_budget field — attributes, never judges.


def attribute_spend(
    runs: Sequence[Mapping[str, object]],
    *,
    productive_statuses: Sequence[str] = DEFAULT_PRODUCTIVE_STATUSES,
) -> SpendAttribution:
    """Attribute recorded run cost to productive vs wasted outcomes.

    Each run is ``{run_id, status, cost}`` with a non-negative ``cost``. Cost on a run whose
    ``status`` is in ``productive_statuses`` is productive; all other cost is wasted. Returns the
    totals, the wasted fraction, and a per-status breakdown (keys sorted for byte-identical output).
    An empty input yields a safe all-zero attribution. Descriptive only — it does not judge whether
    the spend was justified.
    """
    productive_set = set(productive_statuses)
    by_status: dict[str, float] = {}
    total = 0.0
    productive = 0.0
    for run in runs:
        run_id = run.get("run_id")
        status = run.get("status")
        if not isinstance(run_id, str) or not isinstance(status, str):
            raise ValueError("each run needs a string run_id and status")
        raw_cost = run.get("cost")
        if not isinstance(raw_cost, (int, float)) or isinstance(raw_cost, bool):
            raise ValueError(f"run {run_id!r} is missing a numeric cost")
        cost = float(raw_cost)
        if cost < 0:
            raise ValueError(f"run {run_id!r} has negative cost {cost}")

        total += cost
        by_status[status] = by_status.get(status, 0.0) + cost
        if status in productive_set:
            productive += cost

    wasted = total - productive
    wasted_fraction = (wasted / total) if total > 0 else 0.0

    return SpendAttribution(
        total_spend=total,
        productive_spend=productive,
        wasted_spend=wasted,
        wasted_fraction=wasted_fraction,
        productive_statuses=list(productive_statuses),
        by_status=dict(sorted(by_status.items())),
    )
