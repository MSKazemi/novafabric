"""`nova view` — saved views: named, persisted `nova query` definitions (ADR-0130).

Experimental. A saved view is a small, human-readable, version-controllable
file (``.novafabric/views/<view_id>.yaml``, JSON equally valid) wrapping a
verbatim ADR-0129 query object plus optional advisory display preferences.
Data, not code: ``nova view run`` is exactly ``nova query`` over the stored
query (invariant I2) — the saved-view layer adds persistence and ergonomics,
never query power. A broken view fails only its own command and never blocks
any other NovaFabric operation.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, NoReturn, Optional

import typer
import yaml

from novafabric._paths import default_capsule_dir
from novafabric.cli.query import _format_table
from novafabric.query import (
    QueryError,
    QueryParseError,
    run_query,
    validate_query_object,
)
from novafabric.query.parser import load_query_file, plan_from_query_object
from novafabric.views import (
    DisplayPrefs,
    SavedView,
    SortKey,
    ViewError,
    default_views_dir,
    delete_view,
    list_views,
    load_view,
    save_view,
    slugify_view_name,
    view_hash,
)
from novafabric.views.store import resolve_view_file

view_app = typer.Typer(no_args_is_help=True)

_FORMATS = ("table", "json", "csv")

_VIEWS_DIR_OPTION = typer.Option(
    "--views-dir",
    help="Views directory (default: $NOVAFABRIC_VIEWS_DIR or ./.novafabric/views).",
)


def _fail(message: str, *, code: int) -> NoReturn:
    typer.echo(f"View error: {message}", err=True)
    raise SystemExit(code)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _split_csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items or None


def _parse_sort(value: str | None) -> list[SortKey] | None:
    """Parse ``'field [asc|desc], field2 [asc|desc]'`` into display sort keys."""
    if value is None:
        return None
    keys: list[SortKey] = []
    for token in value.split(","):
        parts = token.split()
        if not parts:
            continue
        if len(parts) == 1:
            keys.append(SortKey(field=parts[0], order="desc"))
        elif len(parts) == 2 and parts[1].lower() in ("asc", "desc"):
            keys.append(SortKey(field=parts[0], order=parts[1].lower()))  # type: ignore[arg-type]
        else:
            _fail(f"invalid --sort entry {token.strip()!r} (expected 'FIELD [asc|desc]')", code=2)
    return keys or None


def _render_csv(columns: list[str], rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow(["" if row.get(c) is None else row.get(c) for c in columns])
    return buffer.getvalue().rstrip("\n")


def _display_columns(view: SavedView, columns: list[str]) -> list[str]:
    """Advisory column selection (I3): keep display columns that really exist."""
    if view.display is None or not view.display.columns:
        return columns
    chosen = [c for c in view.display.columns if c in columns]
    return chosen or columns


def _display_sorted(view: SavedView, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Advisory row re-ordering for rendering (I3); JSON output is untouched."""
    if view.display is None or not view.display.sort:
        return rows
    ordered = list(rows)
    for key in reversed(view.display.sort):
        ordered.sort(
            key=lambda row: (row.get(key.field) is None, row.get(key.field)),
            reverse=key.order == "desc",
        )
    return ordered


@view_app.command("save")
def save_cmd(
    name: Annotated[str, typer.Argument(help="Human-facing view name, e.g. failed-runs-last-7d.")],
    select: Annotated[
        Optional[str],
        typer.Option("--select", help="Aggregates, comma-separated — same grammar as nova query."),
    ] = None,
    where: Annotated[
        Optional[str],
        typer.Option("--where", help="Filter predicates joined by AND (nova query grammar)."),
    ] = None,
    group_by: Annotated[
        Optional[str],
        typer.Option("--group-by", help="Dimensions to group by, comma-separated."),
    ] = None,
    since: Annotated[
        Optional[str],
        typer.Option("--since", help="Start of the time window: 7d, 24h, P30D, or RFC 3339."),
    ] = None,
    until: Annotated[
        Optional[str],
        typer.Option("--until", help="End of the time window (exclusive), RFC 3339."),
    ] = None,
    limit: Annotated[
        Optional[int],
        typer.Option("--limit", help="Max result rows (default 100, ceiling 10000)."),
    ] = None,
    order_by: Annotated[
        Optional[str],
        typer.Option("--order-by", help="Sort rows: '<alias> [asc|desc]'."),
    ] = None,
    query_file: Annotated[
        Optional[Path],
        typer.Option("--query-file", help="JSON/YAML query object to save; flags override fields."),
    ] = None,
    view_id: Annotated[
        Optional[str],
        typer.Option("--view-id", help="Explicit view id (slug); derived from NAME if omitted."),
    ] = None,
    description: Annotated[
        Optional[str],
        typer.Option("--description", help="Free-text note on the view's intent."),
    ] = None,
    columns: Annotated[
        Optional[str],
        typer.Option("--columns", help="Advisory display columns, comma-separated."),
    ] = None,
    sort: Annotated[
        Optional[str],
        typer.Option("--sort", help="Advisory display sort: 'FIELD [asc|desc], ...'."),
    ] = None,
    display_format: Annotated[
        Optional[str],
        typer.Option("--format", help="Advisory render format: table, json, or csv."),
    ] = None,
    tags: Annotated[
        Optional[str],
        typer.Option("--tags", help="Free-text labels, comma-separated."),
    ] = None,
    created_by: Annotated[
        Optional[str],
        typer.Option("--created-by", help="Author identity to record; unset means null."),
    ] = None,
    json_file: Annotated[
        bool,
        typer.Option("--json", help="Write a .json view file instead of YAML."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite an existing view (preserves created_at)."),
    ] = False,
    views_dir: Annotated[Optional[Path], _VIEWS_DIR_OPTION] = None,
) -> None:
    """Persist a nova query under a name — validated fail-closed at save time.

    \b
    Examples:
      nova view save failed-runs-7d --select 'count()' \\
        --where 'status = error' --since 7d --description 'Failed runs, last week'
      nova view save cost-by-model --query-file q.yaml --columns 'model,avg_cost' --json
    """
    try:
        query_object = load_query_file(query_file) if query_file is not None else {}
        plan = plan_from_query_object(
            query_object,
            select=select,
            where=where,
            group_by=group_by,
            since=since,
            until=until,
            limit=limit,
            order_by=order_by,
        )
    except QueryParseError as exc:
        typer.echo(f"Query error: {exc}", err=True)
        raise SystemExit(2) from exc

    if display_format is not None and display_format not in _FORMATS:
        _fail(f"--format must be one of {', '.join(_FORMATS)}, got {display_format!r}", code=2)

    display: DisplayPrefs | None = None
    display_columns = _split_csv(columns)
    display_sort = _parse_sort(sort)
    if display_columns or display_sort or display_format:
        display = DisplayPrefs(
            columns=display_columns,
            sort=display_sort,
            format=display_format,  # type: ignore[arg-type]
        )

    try:
        resolved_id = view_id if view_id is not None else slugify_view_name(name)
        view = SavedView(
            view_id=resolved_id,
            name=name,
            query=plan.to_query_object(),
            created_at=_now_iso(),
            description=description,
            created_by=created_by,
            display=display,
            tags=_split_csv(tags),
        )
        path = save_view(
            view,
            views_dir if views_dir is not None else default_views_dir(),
            as_json=json_file,
            force=force,
        )
    except QueryParseError as exc:
        typer.echo(f"Query error: {exc}", err=True)
        raise SystemExit(2) from exc
    except ValueError as exc:  # pydantic envelope validation (e.g. bad --view-id)
        _fail(str(exc), code=2)
    except ViewError as exc:
        _fail(str(exc), code=1)
    typer.echo(f"Saved view '{view.view_id}' -> {path}")
    typer.echo(f"view_hash: {view_hash(view)}")


@view_app.command("run")
def run_cmd(
    name: Annotated[str, typer.Argument(help="View id or name to run.")],
    capsule_dir: Annotated[
        Optional[Path],
        typer.Option(
            "--capsule-dir",
            help="Capsule storage directory (defaults to $NOVAFABRIC_CAPSULE_DIR).",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the canonical JSON result (overrides display prefs)."),
    ] = False,
    output_format: Annotated[
        Optional[str],
        typer.Option("--format", help="Render format: table, json, csv (overrides display prefs)."),
    ] = None,
    views_dir: Annotated[Optional[Path], _VIEWS_DIR_OPTION] = None,
) -> None:
    """Execute a saved view's stored query — exactly `nova query` over it (I2)."""
    if output_format is not None and output_format not in _FORMATS:
        _fail(f"--format must be one of {', '.join(_FORMATS)}, got {output_format!r}", code=2)
    try:
        view = load_view(name, views_dir)
    except ViewError as exc:
        _fail(str(exc), code=1)
    try:
        plan = validate_query_object(view.query, source=f"saved view {view.view_id!r}")
    except QueryParseError as exc:
        typer.echo(f"Query error: {exc}", err=True)
        raise SystemExit(2) from exc

    base = (capsule_dir or default_capsule_dir()).resolve()
    try:
        result = run_query(plan, base)
    except QueryError as exc:
        typer.echo(f"Query error: {exc}", err=True)
        raise SystemExit(1) from exc

    result["view"] = {
        "view_id": view.view_id,
        "name": view.name,
        "view_hash": view_hash(view),
    }

    fmt = output_format or ("json" if json_output else None)
    if fmt is None and view.display is not None and view.display.format:
        fmt = view.display.format
    fmt = fmt or "table"

    if fmt == "json":
        typer.echo(json.dumps(result))
        return

    rendered_columns = _display_columns(view, result["columns"])
    rendered_rows = _display_sorted(view, result["rows"])
    if fmt == "csv":
        typer.echo(_render_csv(rendered_columns, rendered_rows))
        return
    typer.echo(_format_table(rendered_columns, rendered_rows))
    typer.echo(
        f"\n{result['row_count']} row(s)"
        f"{' (truncated)' if result['truncated'] else ''} — "
        f"view '{view.view_id}' ({result['view']['view_hash'][:19]}…), "
        f"{result['index']['capsule_count']} capsule(s) scanned, "
        f"engine {result['index']['engine']}"
    )


@view_app.command("list")
def list_cmd(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the listing as JSON.")
    ] = False,
    views_dir: Annotated[Optional[Path], _VIEWS_DIR_OPTION] = None,
) -> None:
    """List saved views in the project (empty when none — never an error)."""
    views, warnings = list_views(views_dir)
    for warning in warnings:
        typer.echo(f"Warning: skipped unreadable view: {warning}", err=True)
    if json_output:
        typer.echo(
            json.dumps(
                [
                    {
                        "view_id": v.view_id,
                        "name": v.name,
                        "description": v.description,
                        "tags": v.tags or [],
                        "view_hash": view_hash(v),
                    }
                    for v in views
                ]
            )
        )
        return
    if not views:
        typer.echo("(no views)")
        return
    rows = [
        {
            "view_id": v.view_id,
            "name": v.name,
            "description": v.description or "-",
            "tags": ", ".join(v.tags) if v.tags else "-",
        }
        for v in views
    ]
    typer.echo(_format_table(["view_id", "name", "description", "tags"], rows))


@view_app.command("show")
def show_cmd(
    name: Annotated[str, typer.Argument(help="View id or name to show.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the view (plus hash and path) as JSON.")
    ] = False,
    views_dir: Annotated[Optional[Path], _VIEWS_DIR_OPTION] = None,
) -> None:
    """Print a stored view — query, prefs, metadata. Never executes it."""
    try:
        path = resolve_view_file(name, views_dir)
        view = load_view(name, views_dir)
    except ViewError as exc:
        _fail(str(exc), code=1)
    if json_output:
        typer.echo(
            json.dumps(
                {"view": view.to_document(), "view_hash": view_hash(view), "path": str(path)}
            )
        )
        return
    typer.echo(yaml.safe_dump(view.to_document(), sort_keys=False).rstrip("\n"))
    typer.echo(f"# view_hash: {view_hash(view)}")
    typer.echo(f"# path: {path}")


@view_app.command("rm")
def rm_cmd(
    name: Annotated[str, typer.Argument(help="View id or name to delete.")],
    views_dir: Annotated[Optional[Path], _VIEWS_DIR_OPTION] = None,
) -> None:
    """Delete a saved view's file."""
    try:
        path = delete_view(name, views_dir)
    except ViewError as exc:
        _fail(str(exc), code=1)
    typer.echo(f"Removed view file {path}")
