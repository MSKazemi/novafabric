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

Two POST routes are the write path (experimental) — the UI's equivalent of
``nova dashboard validate`` / ``nova dashboard apply``:

- ``POST /api/dashboards/validate`` — the dry run. Runs the CLI's own loaders and
  returns the verdict, the bytes that *would* be stored and the bytes on disk now
  (for a diff). Never writes — not even the directory. Scope ``read``: it
  computes and returns, like ``POST /api/query``.
- ``POST /api/dashboards/apply`` — validate, then store through
  :class:`~novafabric.dashboards.DashboardStore` (atomic temp-file + rename, no-op
  when the bytes already match). Scope ``operate`` (project files, not evidence;
  the saved-views and holds precedent) and **every outcome is audited** —
  written, unchanged, refused, failed — with the id, size and digest but never
  the document body.

**No second validator.** The schema and the ADR-0129 DSL allow-list decide
(ADR-0235 D7) via ``load_widget`` / ``load_dashboard``; this module adds only
what a *network* caller needs that a local file does not: a body size cap checked
while streaming (413 before anything is parsed), an id re-checked against the path
pattern and contained to the directory, a refusal to follow a symlink, a refusal
to write ``builtin`` documents (ADR-0235 D5: the first edit forks a user-owned
copy), and an optional ``base_sha256`` so a preview that went stale is a 409
rather than a silent overwrite (``""`` means "I expected no file").

The reads stay read-only: a missing dashboards directory is reported as empty,
never created, and is guarded by the router-level auth dependency and classified
``read`` in :data:`novafabric.serve.authz.ROUTE_SCOPES`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from collections.abc import Callable
from pathlib import Path as FsPath
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request
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
_ID_RE = re.compile(ID_PATTERN)

#: Largest request body the write routes will read. Real widgets are a few KB;
#: this is generous for a hand-written dashboard and small enough that a paste
#: of the wrong file is refused rather than parsed.
MAX_BODY_BYTES = 256 * 1024


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


class DashboardWriteResponse(BaseModel):
    ok: bool
    kind: str
    id: str
    file: str
    changed: bool
    sha256: str
    warnings: list[str]
    cli_equivalent: str


class DashboardValidateResponse(BaseModel):
    ok: bool
    error: str | None = None
    kind: str | None = None
    id: str | None = None
    title: str | None = None
    action: str | None = None
    normalized: str | None = None
    existing: str | None = None
    proposed_sha256: str | None = None
    current_sha256: str | None = None
    warnings: list[str] = []
    cli_equivalent: str | None = None


_WRITE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"model": DashboardWriteResponse},
    409: {"description": "base_sha256 no longer matches the file on disk (stale preview)."},
    413: {"description": f"Body larger than {MAX_BODY_BYTES} bytes; nothing was parsed."},
    422: {"description": "Refused (schema, DSL, id, built-in, symlink). Nothing written."},
    500: {"description": "The write failed; no partial or temporary file is left."},
}


class _Refusal(Exception):
    """A document the write path will not store, with the HTTP status to answer."""

    def __init__(self, reason: str, status: int = 422) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _read_capped(request: Request) -> bytes:
    """Read the body, refusing as soon as it passes the cap (never buffers more)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise _Refusal(f"body is {declared} bytes; the limit is {MAX_BODY_BYTES}", 413)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise _Refusal(f"body exceeds the {MAX_BODY_BYTES}-byte limit", 413)
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_request(raw: bytes) -> tuple[Any, str | None, str | None]:
    """``(document, kind, base_sha256)`` from the request body, or a refusal."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _Refusal(f"request body is not valid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise _Refusal("request body must be a JSON object")
    text, document = body.get("text"), body.get("document")
    if (text is None) == (document is None):
        raise _Refusal("send exactly one of `text` (raw JSON) or `document`")
    if text is not None:
        if not isinstance(text, str):
            raise _Refusal("`text` must be a string")
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _Refusal(f"document is not valid JSON: {exc}") from exc
    kind = body.get("kind")
    if kind not in (None, "widget", "dashboard"):
        raise _Refusal("`kind` must be 'widget' or 'dashboard'")
    base = body.get("base_sha256")
    if base is not None and not isinstance(base, str):
        raise _Refusal("`base_sha256` must be a string")
    return document, kind, base


def _evaluate(store: DashboardStore, document: Any, kind: str | None) -> dict[str, Any]:
    """Validate with the CLI's own loaders and compare with the file on disk.

    Reads only. Raises :class:`_Refusal` with the validator's message.
    """
    if kind is None:
        # Same rule as `apply_path`: the marker decides, else it is a widget.
        marked = isinstance(document, dict) and document.get("$novafabricDashboard")
        kind = "dashboard" if marked else "widget"
    try:
        item: Widget | Dashboard = (
            load_dashboard(document) if kind == "dashboard" else load_widget(document)
        )
    except DashboardError as exc:
        raise _Refusal(str(exc)) from exc
    if not _ID_RE.fullmatch(item.id):  # defence in depth: the schema already says so
        raise _Refusal(f"id {item.id!r} does not match {ID_PATTERN}")
    path = store.dashboard_path(item.id) if kind == "dashboard" else store.widget_path(item.id)
    if path.parent != store.root:  # pragma: no cover - unreachable for a matching id
        raise _Refusal("id would resolve outside the dashboards directory")
    if item.raw.get("builtin"):
        raise _Refusal(
            f"{item.id!r} is marked built-in, which is read-only (ADR-0235 D5); "
            "remove `builtin` to save a user-owned copy"
        )
    existing: str | None = None
    if path.is_symlink():
        raise _Refusal(f"{path.name} is a symlink; refusing to write through it")
    if path.is_file():
        existing = path.read_text(encoding="utf-8", errors="replace")
        try:
            on_disk = json.loads(existing)
        except json.JSONDecodeError:
            on_disk = None  # a corrupt file may be overwritten; that is how it is fixed
        if isinstance(on_disk, dict) and on_disk.get("builtin"):
            raise _Refusal(f"{path.name} is a built-in document and is read-only (ADR-0235 D5)")
    normalized = item.to_json()
    warnings: list[str] = []
    if isinstance(item, Dashboard):
        missing = store.unresolved_widget_ids(item)
        if missing:
            warnings.append(
                "references widgets with no file yet: "
                + ", ".join(missing)
                + " (saved anyway, as `nova dashboard apply` does; the view marks them missing)"
            )
    return {
        "ok": True,
        "kind": kind,
        "id": item.id,
        "title": item.title,
        "action": "create"
        if existing is None
        else ("unchanged" if existing == normalized else "update"),
        "normalized": normalized,
        "existing": existing,
        "proposed_sha256": _sha256(normalized),
        "current_sha256": None if existing is None else _sha256(existing),
        "warnings": warnings,
        "cli_equivalent": f"nova dashboard show {item.id}",
        "_item": item,
    }


def build_dashboards_router(
    verify_token: Callable[..., Any],
    *,
    capsule_dir: FsPath,
    audit_append: Callable[..., dict[str, Any]],
    dashboards_root: Callable[[], FsPath] = dashboards_dir,
) -> APIRouter:
    """Build the dashboards router.

    ``dashboards_root`` is resolved per request, so ``NOVAFABRIC_HOME`` is
    honoured exactly as ``nova dashboard`` honours it; ``capsule_dir`` anchors
    widget execution like every other ``serve`` read.
    """
    router = APIRouter(dependencies=[Depends(verify_token)], tags=["dashboards"])
    write_lock = threading.Lock()  # one writer at a time; the temp-file swap is per file

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

    @router.post(
        "/api/dashboards/validate",
        operation_id="dashboardValidateDocument",
        responses={
            200: {"model": DashboardValidateResponse},
            413: _WRITE_RESPONSES[413],
        },
        response_model=None,
    )
    async def validate_document(request: Request) -> dict[str, Any]:
        """Dry run: the verdict and the diff inputs. Never writes."""
        try:
            document, kind, _ = _parse_request(await _read_capped(request))
        except _Refusal as exc:
            if exc.status == 413:
                raise HTTPException(status_code=413, detail=exc.reason) from exc
            return {"ok": False, "error": exc.reason, "warnings": []}
        try:
            verdict = await asyncio.to_thread(_evaluate, _store(), document, kind)
        except _Refusal as exc:
            return {"ok": False, "error": exc.reason, "warnings": []}
        verdict.pop("_item")
        return verdict

    @router.post(
        "/api/dashboards/apply",
        operation_id="dashboardApplyDocument",
        responses=_WRITE_RESPONSES,
        response_model=None,
    )
    async def apply_document(
        request: Request,
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        """Validate, then store atomically. Audited whatever the outcome."""
        size = 0
        ident: dict[str, Any] = {}

        def _audit(result: str, error: str | None = None, **args: Any) -> None:
            known = "id" in ident
            audit_append(
                action="dashboard_apply",
                args={**ident, "bytes": size, **args},
                cli_equivalent=f"nova dashboard show {ident['id']}"
                if known
                else "nova dashboard apply",
                actor_token_fp=actor_fp,
                result=result,
                error=error[:500] if error else None,
                resource=f"{ident['kind']}:{ident['id']}" if known else None,
            )

        def _refuse(exc: _Refusal) -> HTTPException:
            _audit("refused", exc.reason)
            return HTTPException(status_code=exc.status, detail=exc.reason)

        try:
            raw = await _read_capped(request)
            size = len(raw)
            document, kind, base = _parse_request(raw)
        except _Refusal as exc:
            raise _refuse(exc) from exc
        if isinstance(document, dict) and isinstance(document.get("id"), str):
            ident["id"] = document["id"][:80]
            ident["kind"] = kind or (
                "dashboard" if document.get("$novafabricDashboard") else "widget"
            )

        def _write() -> dict[str, Any]:
            store = _store()
            with write_lock:
                verdict = _evaluate(store, document, kind)
                if base is not None and base != (verdict["current_sha256"] or ""):
                    raise _Refusal(
                        "the file changed since this preview was taken; review it again "
                        "(nothing was written)",
                        409,
                    )
                item = verdict["_item"]
                if verdict["kind"] == "dashboard":
                    written, changed = store.save_dashboard(item)
                else:
                    written, changed = store.save_widget(item)
            return {**verdict, "file": written.name, "changed": changed}

        try:
            result = await asyncio.to_thread(_write)
        except _Refusal as exc:
            raise _refuse(exc) from exc
        except OSError as exc:
            _audit("error", f"write failed: {exc}")
            raise HTTPException(status_code=500, detail=f"write failed: {exc}") from exc
        ident.update(kind=result["kind"], id=result["id"])
        _audit(
            "ok",
            changed=result["changed"],
            action=result["action"],
            sha256=result["proposed_sha256"],
        )
        return {
            "ok": True,
            "kind": result["kind"],
            "id": result["id"],
            "file": result["file"],
            "changed": result["changed"],
            "sha256": result["proposed_sha256"],
            "warnings": result["warnings"],
            "cli_equivalent": result["cli_equivalent"],
        }

    return router
