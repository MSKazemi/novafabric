"""Dashboard query panel — `POST /api/query` (ADR-0129 read surface, ADR-0183 pattern).

Wraps the Capsule Query DSL's own execution path
(:func:`novafabric.query.executor.run_query`) with **zero** new query
grammar: the request body's ``q`` is the same JSON/YAML query-object document
``nova query --query-file`` already accepts (``the private design/spec/capsule-query-dsl-v0.md``),
not a free-text SQL-like string — ADR-0129 defines no single unified query
string, only the closed ``select`` / ``where`` / ``group_by`` / time-window
clauses. Reusing :func:`novafabric.query.parser.validate_query_object` for
parsing means this panel is exactly as injection-safe and closed-allow-list
as the CLI: nothing here adds a new parsing surface.

Errors: a document that is not valid JSON/YAML, does not decode to an
object, or fails the allow-list (unknown clause/field/operator/function,
unknown engine) is a 422 with the parser's own message. An index-build
failure (:class:`~novafabric.query.errors.QueryIndexError` — a malformed
on-disk capsule) is a 500: it is an environment/data problem, not a bad
request.

Read-only end to end (no writes, no subprocess); not audit-logged — running
a bounded local aggregate query is not a mutating or boundary-crossing
action (unlike, say, a compliance export).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from novafabric.query import (
    QueryExecutionError,
    QueryIndexError,
    QueryParseError,
    QueryPlan,
    run_query,
    validate_query_object,
)

#: Router-level bound on returned rows, independent of the plan's own
#: ``limit`` (which a caller can raise up to ``MAX_LIMIT`` = 10_000). Keeps
#: the dashboard response bounded regardless of what the query asked for.
ROW_CAP = 5000


class QueryPanelRequest(BaseModel):
    """``{q, engine?}`` — ``q`` is a Capsule Query DSL document (JSON or YAML text)."""

    q: str
    engine: str | None = None


def _parse_query_document(text: str) -> dict[str, Any]:
    """Decode ``text`` as a JSON or YAML query-object document.

    Raises :class:`QueryParseError` uniformly (blank text, invalid syntax,
    or a non-object document) so the caller only needs one except clause.
    """
    stripped = text.strip()
    if not stripped:
        raise QueryParseError("query body 'q' must not be empty")
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        try:
            obj = yaml.safe_load(stripped)
        except yaml.YAMLError as exc:
            raise QueryParseError(f"'q' is not valid JSON/YAML: {exc}") from exc
    if not isinstance(obj, dict):
        raise QueryParseError("'q' must decode to a single JSON/YAML object")
    return obj


def _cli_equivalent(plan: QueryPlan) -> str:
    """Reconstruct the `nova query` invocation that would produce this plan."""
    parts = ["nova", "query", "--select", _shq(", ".join(a.normalized() for a in plan.selects))]
    if plan.where:
        parts += ["--where", _shq(" AND ".join(p.normalized() for p in plan.where))]
    if plan.group_by:
        parts += ["--group-by", ",".join(plan.group_by)]
    if plan.since is not None:
        parts += ["--since", plan.since]
    if plan.until is not None:
        parts += ["--until", plan.until]
    parts += ["--limit", str(plan.limit)]
    if plan.order_by.by:
        parts += ["--order-by", _shq(f"{plan.order_by.by} {plan.order_by.direction}")]
    return " ".join(parts)


def _shq(value: str) -> str:
    """Single-quote a value for display in a copy-as-CLI chip."""
    return f"'{value}'"


def build_query_panel_router(
    verify_token: Callable[..., Any],
    *,
    capsule_dir: Path,
) -> APIRouter:
    """Build the query-panel router.

    ``capsule_dir`` anchors the local scan :func:`run_query` performs — the
    same directory every other ``serve`` read endpoint uses.
    """
    router = APIRouter(dependencies=[Depends(verify_token)], tags=["query"])

    @router.post("/api/query")
    async def run_query_endpoint(body: QueryPanelRequest = Body(...)) -> dict[str, Any]:
        try:
            query_object = _parse_query_document(body.q)
            plan = validate_query_object(query_object)
        except QueryParseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        try:
            result = run_query(plan, capsule_dir, engine=body.engine)
        except (QueryParseError, QueryExecutionError, ValueError) as exc:
            # ValueError: an unrecognised `engine` value (QueryIndex.build's
            # own defensive branch raises plain ValueError, not a QueryError).
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except QueryIndexError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        rows = result["rows"]
        capped_rows = rows[:ROW_CAP]
        truncated = bool(result["truncated"]) or len(rows) > ROW_CAP
        return {
            **result,
            "rows": capped_rows,
            "row_count": len(capped_rows),
            "truncated": truncated,
            "cli_equivalent": _cli_equivalent(plan),
        }

    return router
