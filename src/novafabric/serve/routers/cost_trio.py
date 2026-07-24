"""Cost-analytics trio dashboard read surface (ADR-0201 P6, ADR-0183 pattern).

Three POST endpoints, each wrapping one pure, deterministic cost core with
**no** new computation of its own — the same functions the ``nova cost``
subcommands call, given a request document instead of a file path:

- ``POST /api/cost/attribute`` → :func:`novafabric.cost.spend_attribution.attribute_spend`
  (``nova cost attribute`` — productive-vs-wasted spend, ADR-0146 D3)
- ``POST /api/cost/fairness``        → :func:`novafabric.cost.fairness.build_fairness_report`
  (``nova cost fairness`` — per-agent share/Gini, ADR-0146 D5)
- ``POST /api/cost/usage-breakdown`` →
  :func:`novafabric.cost.usage_breakdown.compute_usage_breakdown`
  (``nova cost usage-breakdown`` — token usage-type composition, ADR-0132)

Each accepts exactly the document shape its CLI command accepts, validates it
with the CLI's own error semantics, and returns the core model's
``model_dump(mode="json")``. A malformed request document is a 422 with the
core's own message — these are **descriptive** analytics, never a quota or a
judgement (the cores say so in their own docstrings), and the endpoints add no
new interpretation. Read-only end to end (pure functions, no capsule IO, no
subprocess, no writes); not audit-logged — computing a bounded aggregate over
caller-supplied numbers is not a mutating or boundary-crossing action.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from novafabric.cost.fairness import build_fairness_report
from novafabric.cost.spend_attribution import attribute_spend
from novafabric.cost.usage_breakdown import compute_usage_breakdown


class AttributeRequest(BaseModel):
    """``{runs:[{run_id,status,cost}], productive_statuses?}`` — ``nova cost attribute``."""

    runs: list[dict[str, Any]]
    productive_statuses: list[str] = Field(default_factory=lambda: ["success"])


class FairnessRequest(BaseModel):
    """``{totals: {dimension: {agent: total}}}`` — ``nova cost fairness``."""

    totals: dict[str, dict[str, float]]


class UsageBreakdownRequest(BaseModel):
    """``{usage_totals: {...}}`` (or a bare usage-totals map) — ``nova cost usage-breakdown``."""

    usage_totals: dict[str, Any]


def build_cost_trio_router(verify_token: Callable[..., Any]) -> APIRouter:
    """Build the cost-analytics-trio router (three pure POST compute endpoints)."""
    router = APIRouter(dependencies=[Depends(verify_token)], tags=["cost"])

    @router.post("/api/cost/attribute")
    async def attribute_endpoint(body: AttributeRequest = Body(...)) -> dict[str, Any]:
        try:
            report = attribute_spend(
                body.runs, productive_statuses=[str(s) for s in body.productive_statuses]
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return report.model_dump(mode="json")

    @router.post("/api/cost/fairness")
    async def fairness_endpoint(body: FairnessRequest = Body(...)) -> dict[str, Any]:
        try:
            report = build_fairness_report(body.totals)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return report.model_dump(mode="json")

    @router.post("/api/cost/usage-breakdown")
    async def usage_breakdown_endpoint(
        body: UsageBreakdownRequest = Body(...),
    ) -> dict[str, Any]:
        try:
            breakdown = compute_usage_breakdown(body.usage_totals)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return breakdown.model_dump(mode="json")

    return router
