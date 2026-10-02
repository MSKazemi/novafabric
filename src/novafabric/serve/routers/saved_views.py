# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Dashboard saved views as ADR-0130 ``nova view`` entries — ADR-0232 D4 (experimental).

    "Save this view" writes an ADR-0130 saved view containing the same
    ``QueryPlan``. No parallel persistence. … A saved view created in the
    dashboard is readable by ``nova view show`` and runnable by ``nova view run``.

So this router adds **no store of its own**: it reads and writes the same
``.novafabric/views/`` files ``nova view`` does, through
:mod:`novafabric.views` (save is fail-closed through the ADR-0129 parser).

- ``GET    /api/views``            — every view, each with a ``dashboard`` block
  when its query has a Runs-view form (else the reason it has none).
- ``POST   /api/views``            — save the Runs view state as a view.
- ``DELETE /api/views/{view_id}``  — remove one (``nova view rm``).

## How the Runs view maps onto a query object

=============== ==============================================================
Runs view       ADR-0129 query object
=============== ==============================================================
filter bar ``f`` ``where`` — the predicates :func:`parse_filter_bar` compiles
status chip     one more ``status = <chip>`` predicate (the bar's ``status:``)
scope           ``scope`` (``node`` omitted)
date window     ``since`` / ``until``
sort            ``display.sort`` (advisory, ADR-0130 I3)
search ``q``    **not saved** — free-text search over run ids/commands is not
                expressible in ``nova query``, and saving it would be a second,
                unreviewed query surface (ADR-0232 D1). The UI says so.
=============== ==============================================================

``select`` is ``count()``: the dashboard lists the runs, and ``nova view run``
counts the rows the same predicates match — the same check the filter bar's
``cli_equivalent`` offers.

Reading back, a view's predicates become filter-bar text again (``dim:value``,
``-dim:value``). A view saved by the CLI with a predicate the bar cannot express
(``IN``, a comparison) is listed with ``dashboard: null`` and the reason, never
silently approximated.

Writes are ``operate`` scope (they change project files, not evidence) and are
recorded in the dashboard audit log.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Literal

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

#: Tag every dashboard-saved view carries, so `nova view list` shows its origin.
DASHBOARD_TAG: Final[str] = "dashboard:runs"

#: Runs-view sort ↔ advisory display sort.
_SORT_TO_DISPLAY: Final[dict[str, tuple[str, str]]] = {
    "newest": ("created_at", "desc"),
    "oldest": ("created_at", "asc"),
    "longest": ("duration_ms", "desc"),
    "shortest": ("duration_ms", "asc"),
}
_DISPLAY_TO_SORT: Final[dict[tuple[str, str], str]] = {
    v: k for k, v in _SORT_TO_DISPLAY.items()
}

_STATUS_CHIPS: Final[frozenset[str]] = frozenset(
    {"all", "running", "success", "failure", "error"}
)


class SaveViewRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    f: str = Field(default="", max_length=2000)
    scope: Literal["node", "root", "tree"] = "node"
    since: str = Field(default="", max_length=64)
    until: str = Field(default="", max_length=64)
    status: str = "all"
    sort: Literal["newest", "oldest", "longest", "shortest"] = "newest"
    description: str | None = Field(default=None, max_length=500)
    overwrite: bool = False


class ViewsListResponse(BaseModel):
    views_dir: str
    views: list[dict[str, Any]]
    warnings: list[str]


class SaveViewResponse(BaseModel):
    ok: bool
    path: str
    view: dict[str, Any]


class DeleteViewResponse(BaseModel):
    ok: bool
    deleted: str


def _quote(value: str) -> str:
    return f'"{value}"' if any(c.isspace() for c in value) or value == "" else value


def dashboard_state(view: Any) -> tuple[dict[str, Any] | None, str | None]:
    """The Runs-view state a saved view reproduces, or ``(None, reason)``."""
    from novafabric.query.errors import QueryParseError
    from novafabric.query.parser import validate_query_object

    try:
        plan = validate_query_object(view.query, source=f"saved view {view.view_id!r}")
    except QueryParseError as exc:
        return None, f"the stored query no longer compiles: {exc}"
    terms: list[str] = []
    for pred in plan.where:
        if pred.op == "=" and pred.value is not None:
            terms.append(f"{pred.dimension}:{_quote(pred.value)}")
        elif pred.op == "!=" and pred.value is not None:
            terms.append(f"-{pred.dimension}:{_quote(pred.value)}")
        else:
            return None, (
                f"predicate '{pred.normalized()}' has no filter-bar form; "
                f"run it with `nova view run {view.view_id}`"
            )
    sort = "newest"
    display = getattr(view, "display", None)
    if display is not None and display.sort:
        key = (display.sort[0].field, display.sort[0].order)
        sort = _DISPLAY_TO_SORT.get(key, "newest")
    scope = getattr(plan, "scope", None)
    scope_value = getattr(scope, "value", scope) or "node"
    return {
        "f": " ".join(terms),
        "scope": scope_value,
        "since": view.query.get("since") or "",
        "until": view.query.get("until") or "",
        "status": "all",
        "sort": sort,
    }, None


def build_saved_views_router(
    verify_token: Callable[..., Any],
    *,
    audit_append: Callable[..., dict[str, Any]],
    views_dir: Callable[[], Path] | None = None,
) -> APIRouter:
    """``views_dir`` resolves the directory per request (default: the same one
    ``nova view`` uses — ``$NOVAFABRIC_VIEWS_DIR`` or ``./.novafabric/views``)."""
    from novafabric.views import default_views_dir

    resolve_dir = views_dir or default_views_dir
    router = APIRouter(tags=["saved-views"])

    def _view_payload(view: Any) -> dict[str, Any]:
        from novafabric.views import view_hash

        state, reason = dashboard_state(view)
        return {
            "view_id": view.view_id,
            "name": view.name,
            "description": view.description,
            "tags": view.tags or [],
            "query": view.query,
            "created_at": view.created_at,
            "updated_at": view.updated_at,
            "view_hash": view_hash(view),
            "dashboard": state,
            "dashboard_unavailable_reason": reason,
            "cli_equivalent": f"nova view run {view.view_id}",
        }

    @router.get(
        "/api/views",
        operation_id="dashboardListSavedViews",
        responses={200: {"model": ViewsListResponse}},
        response_model=None,
        dependencies=[Depends(verify_token)],
    )
    async def list_saved_views() -> dict[str, Any]:
        from novafabric.views import list_views

        base = resolve_dir()
        views, warnings = list_views(base)
        return {
            "views_dir": str(base),
            "views": [_view_payload(v) for v in views],
            "warnings": warnings,
        }

    @router.post(
        "/api/views",
        operation_id="dashboardSaveView",
        responses={200: {"model": SaveViewResponse}},
        response_model=None,
    )
    async def save_saved_view(
        body: SaveViewRequest = Body(...),
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        from novafabric.query import QueryParseError, parse_filter_bar
        from novafabric.views import (
            DisplayPrefs,
            SavedView,
            SortKey,
            ViewError,
            ViewExistsError,
            save_view,
            slugify_view_name,
        )

        if body.status not in _STATUS_CHIPS:
            raise HTTPException(status_code=422, detail=f"unknown status {body.status!r}")
        text = body.f.strip()
        if body.status != "all":
            text = f"{text} status:{body.status}".strip()
        try:
            predicates = parse_filter_bar(text) if text else ()
        except QueryParseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            view_id = slugify_view_name(body.name)
        except ViewError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        query: dict[str, Any] = {"select": ["count()"]}
        if predicates:
            query["where"] = [p.normalized() for p in predicates]
        if body.scope != "node":
            query["scope"] = body.scope
        if body.since:
            query["since"] = body.since
        if body.until:
            query["until"] = body.until
        field, order = _SORT_TO_DISPLAY[body.sort]
        view = SavedView(
            view_id=view_id,
            name=body.name.strip(),
            query=query,
            created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            created_by=f"dashboard:{actor_fp}",
            description=body.description,
            display=DisplayPrefs(sort=[SortKey(field=field, order=order)]),  # type: ignore[arg-type]
            tags=[DASHBOARD_TAG],
        )
        base = resolve_dir()
        try:
            path = save_view(view, base, force=body.overwrite)
        except ViewExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except QueryParseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ViewError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        audit_append(
            action="view_save",
            args={"view_id": view_id, "query": query, "overwrite": body.overwrite},
            cli_equivalent=f"nova view show {view_id}",
            actor_token_fp=actor_fp,
            resource=f"view:{view_id}",
        )
        from novafabric.views import load_view

        return {"ok": True, "path": str(path), "view": _view_payload(load_view(view_id, base))}

    @router.delete(
        "/api/views/{view_id}",
        operation_id="dashboardDeleteView",
        responses={200: {"model": DeleteViewResponse}},
        response_model=None,
    )
    async def delete_saved_view(
        view_id: str,
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        from novafabric.views import ViewNotFoundError, delete_view
        from novafabric.views.model import is_valid_view_id

        if not is_valid_view_id(view_id):
            raise HTTPException(status_code=422, detail="invalid view id")
        try:
            path = delete_view(view_id, resolve_dir())
        except ViewNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        audit_append(
            action="view_delete",
            args={"view_id": view_id},
            cli_equivalent=f"nova view rm {view_id}",
            actor_token_fp=actor_fp,
            resource=f"view:{view_id}",
        )
        return {"ok": True, "deleted": str(path)}

    return router
