"""Dashboard filter bar over HTTP — ADR-0232 D1/D3, ADR-0233 scope, ADR-0234 D2.

Three read-only routes give the dashboard's Runs view the filter grammar that
already ships in :mod:`novafabric.query.filterbar`:

- ``GET /api/filter/parse``   — compile a bar string; 422 names the vocabulary.
- ``GET /api/filter/suggest`` — observed values for one dimension (D3), bounded,
  with ``truncated`` so a partial list never reads as the whole one.
- ``GET /api/filter/runs``    — the runs a bar string selects, widened to an
  ADR-0233 scope, newest first, bounded.

**No new grammar and no new query surface.** Parsing is
:func:`~novafabric.query.filterbar.parse_filter_bar`, which delegates every
predicate to the DSL's own parser; selection is
:func:`~novafabric.query.executor.select_run_ids`, which uses the same index,
predicates and scope expansion as ``nova query``. Every response carries the
``nova query`` invocation that reproduces it, so the bar is a typing convenience
the CLI can always check — never a second, unreviewed query language.

Honest degradation (ADR-0234 D2): a truncated selection, a scope expansion that
hit its bound, or a tree that is still filling is **reported**, never silently
rendered as the complete answer.

Read-only end to end (no writes, no subprocess); guarded by the router-level
auth dependency and classified ``read`` in :data:`novafabric.serve.authz.ROUTE_SCOPES`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from novafabric.query import (
    MAX_SUGGESTIONS,
    QueryExecutionError,
    QueryIndexError,
    QueryParseError,
    observed_values,
    parse_filter_bar,
    select_run_ids,
)
from novafabric.query.cache import scan_capsule_dir_cached
from novafabric.query.executor import resolve_time_window
from novafabric.query.indexer import CallRow, ScoreRow
from novafabric.query.model import DIMENSIONS, Predicate, Scope
from novafabric.serve.capsule_loader import run_summaries_for

#: Hard cap on runs returned by one ``/api/filter/runs`` call (ADR-0199 bound).
MAX_FILTER_RUNS = 200

#: ADR-0232 D3 requires a time window on suggestions; this is the default one.
DEFAULT_SUGGEST_WINDOW = "30d"

#: Upper bound on the raw filter text — a filter bar, not a document.
MAX_FILTER_CHARS = 2000


class FilterParseResponse(BaseModel):
    filter: str
    predicates: list[str]
    where: str
    cli_equivalent: str


class FilterSuggestResponse(BaseModel):
    dimension: str
    values: list[str]
    truncated: bool
    since: str
    until: str


class FilterRunsResponse(BaseModel):
    filter: str
    scope: str
    where: str
    cli_equivalent: str
    matched: int
    truncated: bool
    complete: bool
    incomplete_reasons: list[str]
    since: str
    until: str
    items: list[dict[str, Any]]


def _where(predicates: tuple[Predicate, ...]) -> str:
    return " AND ".join(p.normalized() for p in predicates)


def cli_equivalent(
    predicates: tuple[Predicate, ...],
    *,
    scope: Scope = Scope.NODE,
    since: str | None = None,
    until: str | None = None,
) -> str:
    """The ``nova query`` invocation over the same predicates, scope and window.

    It counts matching *rows* (model calls, or one synthetic row per call-less
    capsule) rather than listing runs — the DSL has no run projection — so it
    is the check that the bar's predicates mean what the CLI's mean.
    """
    parts = ["nova", "query", "--select", "'count()'"]
    if predicates:
        parts += ["--where", f"'{_where(predicates)}'"]
    if scope is not Scope.NODE:
        parts += ["--scope", scope.value]
    if since:
        parts += ["--since", since]
    if until:
        parts += ["--until", until]
    return " ".join(parts)


def _parse_or_422(text: str) -> tuple[Predicate, ...]:
    if len(text) > MAX_FILTER_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"filter is longer than {MAX_FILTER_CHARS} characters",
        )
    try:
        return parse_filter_bar(text)
    except QueryParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _scope_or_422(value: str) -> Scope:
    try:
        return Scope(value)
    except ValueError as exc:
        allowed = ", ".join(s.value for s in Scope)
        raise HTTPException(
            status_code=422, detail=f"unknown scope {value!r}; allowed: {allowed}"
        ) from exc


def build_filter_bar_router(
    verify_token: Callable[..., Any],
    *,
    capsule_dir: Path,
) -> APIRouter:
    """Build the filter-bar router; ``capsule_dir`` is the store every read uses."""
    router = APIRouter(dependencies=[Depends(verify_token)], tags=["filter"])

    @router.get(
        "/api/filter/parse",
        operation_id="dashboardParseFilter",
        responses={200: {"model": FilterParseResponse}},
        response_model=None,
    )
    async def parse_filter(
        f: str = Query(default="", description="Filter-bar text, e.g. status:error -model:gpt-4"),
    ) -> dict[str, Any]:
        predicates = _parse_or_422(f)
        return {
            "filter": f,
            "predicates": [p.normalized() for p in predicates],
            "where": _where(predicates),
            "cli_equivalent": cli_equivalent(predicates),
        }

    @router.get(
        "/api/filter/suggest",
        operation_id="dashboardSuggestFilterValues",
        responses={200: {"model": FilterSuggestResponse}},
        response_model=None,
    )
    async def suggest_values(
        dimension: str = Query(description="One of the DSL's filterable dimensions"),
        since: str = Query(default=DEFAULT_SUGGEST_WINDOW),
        until: str | None = Query(default=None),
        limit: int = Query(default=MAX_SUGGESTIONS, ge=1, le=MAX_SUGGESTIONS),
    ) -> dict[str, Any]:
        if dimension not in DIMENSIONS:
            raise HTTPException(
                status_code=422,
                detail=f"unknown dimension {dimension!r}; allowed: {', '.join(DIMENSIONS)}",
            )
        try:
            since_epoch, until_epoch, since_iso, until_iso = resolve_time_window(
                since, until, datetime.now(timezone.utc)
            )
        except QueryParseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        def _scan() -> dict[str, Any]:
            if not capsule_dir.is_dir():
                return {"dimension": dimension, "values": [], "truncated": False}
            rows = scan_capsule_dir_cached(capsule_dir)
            every: list[CallRow | ScoreRow] = [*rows.calls, *rows.scores]
            in_window = (
                r
                for r in every
                if (since_epoch is None or r.created_at >= since_epoch)
                and r.created_at <= until_epoch
            )
            return observed_values(dimension, in_window, limit=limit).as_dict()

        try:
            result = await asyncio.to_thread(_scan)
        except QueryIndexError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {**result, "since": since_iso, "until": until_iso}

    @router.get(
        "/api/filter/runs",
        operation_id="dashboardFilterRuns",
        responses={200: {"model": FilterRunsResponse}},
        response_model=None,
    )
    async def filter_runs(
        f: str = Query(default="", description="Filter-bar text"),
        scope: str = Query(default="node", description="ADR-0233 scope: node | root | tree"),
        since: str | None = Query(default=None),
        until: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=MAX_FILTER_RUNS),
    ) -> dict[str, Any]:
        predicates = _parse_or_422(f)
        scope_value = _scope_or_422(scope)

        def _select() -> dict[str, Any]:
            base = {
                "filter": f,
                "scope": scope_value.value,
                "where": _where(predicates),
                "cli_equivalent": cli_equivalent(
                    predicates, scope=scope_value, since=since, until=until
                ),
            }
            if not capsule_dir.is_dir():
                _, _, since_iso, until_iso = resolve_time_window(
                    since, until, datetime.now(timezone.utc)
                )
                return {
                    **base, "matched": 0, "truncated": False, "complete": True,
                    "incomplete_reasons": [], "since": since_iso, "until": until_iso,
                    "items": [],
                }
            selection = select_run_ids(
                predicates, capsule_dir, scope=scope_value,
                since=since, until=until, limit=limit,
            )
            items = run_summaries_for(capsule_dir, list(selection.run_ids))
            reasons = list(selection.incomplete_reasons)
            if len(items) < len(selection.run_ids):
                reasons.append(
                    f"{len(selection.run_ids) - len(items)} selected run(s) have no "
                    "readable capsule manifest and are not listed"
                )
            return {
                **base,
                "matched": selection.matched,
                "truncated": selection.truncated,
                "complete": not reasons and not selection.truncated,
                "incomplete_reasons": reasons,
                "since": selection.since_iso,
                "until": selection.until_iso,
                "items": items,
            }

        try:
            return await asyncio.to_thread(_select)
        except (QueryParseError, QueryExecutionError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except QueryIndexError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return router
