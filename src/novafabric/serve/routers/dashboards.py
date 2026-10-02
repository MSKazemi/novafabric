"""Dashboards over HTTP — ADR-0235 portable widgets, ADR-0236 ``ratio()``.

Five read-only routes give the dashboard's *Dashboards* view the files that
``nova dashboard`` already manages under ``$NOVAFABRIC_HOME/dashboards``:

- ``GET /api/dashboards``                          — every dashboard and widget on disk.
- ``GET /api/dashboards/{dashboard_id}``           — one dashboard, every reference resolved.
- ``GET /api/dashboards/{dashboard_id}/export``    — the file's bytes, verbatim.
- ``GET /api/dashboard-widgets/{widget_id}/data``  — run the widget's own query.
- ``GET /api/dashboard-widgets/{widget_id}/export``— the file's bytes, verbatim.

**No new storage and no new grammar.** Reads go through
:class:`~novafabric.dashboards.DashboardStore`, so every document is validated
against its JSON Schema and then the ADR-0129 DSL *before* anything uses it
(ADR-0235 D7) — exactly as the CLI does. Execution is
:func:`novafabric.query.run_query` over the widget's stored query; the request
carries no query text at all, so this surface cannot express anything the
widget file (and therefore ``nova query``) could not.

**One bad file never hides the rest — and is never silently dropped.**
``nova dashboard list`` exits on the first invalid file; a view that did the
same would turn one malformed paste into an empty page. The listing instead
reports each refused file by name with the validator's message, and a
dashboard detail reports each reference as ``ok`` / ``missing`` / ``invalid``.
A dashboard that renders three of four panels and says nothing is the partial
answer ADR-0234 exists to prevent.

**Export is the bytes on disk** (ADR-0235 D6): the response body is
``to_json()`` of the stored ``raw`` document, so fields this build does not
recognise survive a download exactly as they survive ``nova dashboard export``.

Writes (``nova dashboard apply``) are deliberately not exposed: a write path
needs an ``operate`` classification and an audit record, and is its own slice.

Read-only end to end (no writes — a missing dashboards directory is reported
as empty, never created); guarded by the router-level auth dependency and
classified ``read`` in :data:`novafabric.serve.authz.ROUTE_SCOPES`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path as FsPath
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import Response
from pydantic import BaseModel

from novafabric._paths import dashboards_dir
from novafabric.dashboards import (
    Dashboard,
    DashboardError,
    DashboardStore,
    Widget,
    load_dashboard,
    load_widget,
)
from novafabric.dashboards._store import DASHBOARD_SUFFIX, WIDGET_SUFFIX, _read_json
from novafabric.query import (
    QueryExecutionError,
    QueryIndexError,
    QueryParseError,
    run_query,
    validate_query_object,
)
from novafabric.serve.routers.query_panel import ROW_CAP, _cli_equivalent

#: Same constraint the schemas put on ``id`` (the one field that reaches a
#: path). Enforced here too so a bad id is a 422 before the store sees it.
ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"


class DashboardSummary(BaseModel):
    id: str
    title: str
    description: str | None
    builtin: bool
    version: int
    widget_count: int
    unresolved_widgets: list[str]
    invalid_widgets: list[str]


class WidgetSummary(BaseModel):
    id: str
    title: str
    description: str | None
    chart: str
    version: int


class InvalidFile(BaseModel):
    file: str
    kind: str
    error: str


class DashboardListResponse(BaseModel):
    dashboards: list[DashboardSummary]
    widgets: list[WidgetSummary]
    invalid_files: list[InvalidFile]
    cli_equivalent: str


class WidgetDefinition(BaseModel):
    id: str
    title: str
    description: str | None
    chart: str
    version: int
    presentation: dict[str, Any]
    query: dict[str, Any]


class DashboardWidgetRef(BaseModel):
    widget: str
    position: dict[str, int] | None
    status: str
    definition: WidgetDefinition | None
    error: str | None


class DashboardDetailResponse(BaseModel):
    dashboard: DashboardSummary
    widgets: list[DashboardWidgetRef]
    cli_equivalent: str


class DerivedColumn(BaseModel):
    alias: str
    func: str
    numerator: str
    denominator: str


class WidgetDataResponse(BaseModel):
    widget: WidgetDefinition
    schema_version: str
    generated_at: str
    query: dict[str, Any]
    time_window: dict[str, Any]
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    index: dict[str, Any]
    derived: list[DerivedColumn]
    cli_equivalent: str


_EXPORT_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "The stored document, byte-for-byte (ADR-0235 D6).",
        "content": {"application/json": {"schema": {"type": "object"}}},
    },
    404: {"description": "No such document."},
    422: {"description": "The stored document fails validation and is refused."},
}


def _widget_definition(widget: Widget) -> dict[str, Any]:
    raw = widget.raw
    return {
        "id": widget.id,
        "title": widget.title,
        "description": raw.get("description"),
        "chart": widget.chart,
        "version": widget.version,
        "presentation": dict(raw["presentation"]),
        "query": widget.query,
    }


def _dashboard_summary(
    dashboard: Dashboard, statuses: dict[str, tuple[str, str | None]]
) -> dict[str, Any]:
    return {
        "id": dashboard.id,
        "title": dashboard.title,
        "description": dashboard.raw.get("description"),
        "builtin": dashboard.builtin,
        "version": dashboard.version,
        "widget_count": len(dashboard.widget_ids),
        "unresolved_widgets": [w for w in dashboard.widget_ids if statuses[w][0] == "missing"],
        "invalid_widgets": [w for w in dashboard.widget_ids if statuses[w][0] == "invalid"],
    }


def _widget_status(
    store: DashboardStore, widget_id: str
) -> tuple[str, str | None, Widget | None]:
    """``ok`` / ``missing`` / ``invalid`` for one reference — never an exception."""
    path = store.widget_path(widget_id)
    if not path.is_file():
        return "missing", f"no widget file {path.name}", None
    try:
        return "ok", None, load_widget(_read_json(path))
    except DashboardError as exc:
        return "invalid", str(exc), None


def _export_response(payload: str, filename: str) -> Response:
    return Response(
        content=payload.encode("utf-8"),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


def build_dashboards_router(
    verify_token: Callable[..., Any],
    *,
    capsule_dir: FsPath,
    dashboards_root: Callable[[], FsPath] = dashboards_dir,
) -> APIRouter:
    """Build the dashboards router.

    ``dashboards_root`` is resolved per request, so ``NOVAFABRIC_HOME`` is
    honoured exactly as ``nova dashboard`` honours it; ``capsule_dir`` anchors
    widget execution like every other ``serve`` read.
    """
    router = APIRouter(dependencies=[Depends(verify_token)], tags=["dashboards"])

    def _store() -> DashboardStore:
        # No mkdir: a read must not create state. A missing root lists empty.
        return DashboardStore(root=dashboards_root())

    def _get_dashboard(store: DashboardStore, dashboard_id: str) -> Dashboard:
        path = store.dashboard_path(dashboard_id)
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"no dashboard {dashboard_id!r}")
        try:
            return load_dashboard(_read_json(path))
        except DashboardError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def _get_widget(store: DashboardStore, widget_id: str) -> Widget:
        status, error, widget = _widget_status(store, widget_id)
        if status == "missing":
            raise HTTPException(status_code=404, detail=f"no widget {widget_id!r}")
        if widget is None:
            raise HTTPException(status_code=422, detail=error)
        return widget

    @router.get(
        "/api/dashboards",
        operation_id="dashboardListDashboards",
        responses={200: {"model": DashboardListResponse}},
        response_model=None,
    )
    async def list_dashboards() -> dict[str, Any]:
        def _list() -> dict[str, Any]:
            store = _store()
            invalid: list[dict[str, str]] = []
            widgets: list[dict[str, Any]] = []
            dashboards: list[dict[str, Any]] = []
            if store.root.is_dir():
                for path in sorted(store.root.glob(f"*{WIDGET_SUFFIX}")):
                    try:
                        widget = load_widget(_read_json(path))
                    except DashboardError as exc:
                        invalid.append({"file": path.name, "kind": "widget", "error": str(exc)})
                        continue
                    definition = _widget_definition(widget)
                    widgets.append({k: definition[k] for k in WidgetSummary.model_fields})
                for path in sorted(store.root.glob(f"*{DASHBOARD_SUFFIX}")):
                    try:
                        dash = load_dashboard(_read_json(path))
                    except DashboardError as exc:
                        invalid.append(
                            {"file": path.name, "kind": "dashboard", "error": str(exc)}
                        )
                        continue
                    statuses = {
                        w: _widget_status(store, w)[:2] for w in dash.widget_ids
                    }
                    dashboards.append(_dashboard_summary(dash, statuses))
            return {
                "dashboards": dashboards,
                "widgets": widgets,
                "invalid_files": invalid,
                "cli_equivalent": "nova dashboard list",
            }

        return await asyncio.to_thread(_list)

    @router.get(
        "/api/dashboards/{dashboard_id}",
        operation_id="dashboardGetDashboard",
        responses={200: {"model": DashboardDetailResponse}},
        response_model=None,
    )
    async def get_dashboard(
        dashboard_id: str = Path(pattern=ID_PATTERN),
    ) -> dict[str, Any]:
        def _detail() -> dict[str, Any]:
            store = _store()
            dash = _get_dashboard(store, dashboard_id)
            refs: list[dict[str, Any]] = []
            statuses: dict[str, tuple[str, str | None]] = {}
            for entry in dash.raw.get("widgets", []):
                widget_id = str(entry["widget"])
                status, error, widget = _widget_status(store, widget_id)
                statuses[widget_id] = (status, error)
                position = entry.get("position")
                refs.append(
                    {
                        "widget": widget_id,
                        "position": dict(position) if isinstance(position, dict) else None,
                        "status": status,
                        "definition": _widget_definition(widget) if widget else None,
                        "error": error,
                    }
                )
            return {
                "dashboard": _dashboard_summary(dash, statuses),
                "widgets": refs,
                "cli_equivalent": f"nova dashboard show {dashboard_id}",
            }

        return await asyncio.to_thread(_detail)

    @router.get(
        "/api/dashboards/{dashboard_id}/export",
        operation_id="dashboardExportDashboard",
        responses=_EXPORT_RESPONSES,
        response_model=None,
    )
    async def export_dashboard(dashboard_id: str = Path(pattern=ID_PATTERN)) -> Response:
        dash = await asyncio.to_thread(_get_dashboard, _store(), dashboard_id)
        return _export_response(dash.to_json(), f"{dash.id}{DASHBOARD_SUFFIX}")

    @router.get(
        "/api/dashboard-widgets/{widget_id}/export",
        operation_id="dashboardExportWidget",
        responses=_EXPORT_RESPONSES,
        response_model=None,
    )
    async def export_widget(widget_id: str = Path(pattern=ID_PATTERN)) -> Response:
        widget = await asyncio.to_thread(_get_widget, _store(), widget_id)
        return _export_response(widget.to_json(), f"{widget.id}{WIDGET_SUFFIX}")

    @router.get(
        "/api/dashboard-widgets/{widget_id}/data",
        operation_id="dashboardWidgetData",
        responses={200: {"model": WidgetDataResponse}},
        response_model=None,
    )
    async def widget_data(widget_id: str = Path(pattern=ID_PATTERN)) -> dict[str, Any]:
        def _run() -> dict[str, Any]:
            widget = _get_widget(_store(), widget_id)
            try:
                plan = validate_query_object(widget.query)
            except QueryParseError as exc:  # pragma: no cover
                # load_widget already ran this; kept so a refusal stays a 422.
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            try:
                result = run_query(plan, capsule_dir)
            except (QueryParseError, QueryExecutionError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except QueryIndexError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            rows = result["rows"]
            capped = rows[:ROW_CAP]
            return {
                **result,
                "rows": capped,
                "row_count": len(capped),
                "truncated": bool(result["truncated"]) or len(rows) > ROW_CAP,
                "widget": _widget_definition(widget),
                # ADR-0236: operands are named so a ratio is never shown
                # without the numerator and denominator it was computed from.
                "derived": [
                    {
                        "alias": agg.alias,
                        "func": agg.func,
                        "numerator": agg.numerator or "",
                        "denominator": agg.denominator or "",
                    }
                    for agg in plan.selects
                    if agg.is_derived
                ],
                "cli_equivalent": _cli_equivalent(plan),
            }

        return await asyncio.to_thread(_run)

    return router
