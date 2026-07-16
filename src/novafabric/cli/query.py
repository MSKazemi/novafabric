"""`nova query` — offline metrics query DSL over local capsules (ADR-0129).

Experimental. Declarative, read-only ``filter → group-by → aggregate`` over
the local capsule directory: no server, no network, no raw SQL. The grammar
is a closed allow-list (``design/spec/capsule-query-dsl-v0.md``); anything
outside it is a hard parse error.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Optional

import typer

from novafabric._paths import default_capsule_dir
from novafabric.query import (
    QueryError,
    QueryParseError,
    plan_from_query_object,
    run_query,
)
from novafabric.query.parser import load_query_file


def _format_table(columns: list[str], rows: list[dict[str, Any]]) -> str:
    """Render the canonical result rows as a plain-text table."""
    if not rows:
        return "(no rows)"

    def cell(value: Any) -> str:
        if value is None:
            return "-"
        if isinstance(value, float):
            return f"{value:.6g}"
        return str(value)

    widths = {c: len(c) for c in columns}
    rendered = [{c: cell(r.get(c)) for c in columns} for r in rows]
    for row in rendered:
        for c in columns:
            widths[c] = max(widths[c], len(row[c]))
    sep = "  "
    lines = [
        sep.join(c.ljust(widths[c]) for c in columns),
        sep.join("-" * widths[c] for c in columns),
    ]
    lines.extend(sep.join(row[c].ljust(widths[c]) for c in columns) for row in rendered)
    return "\n".join(lines)


def query_cmd(
    select: Annotated[
        Optional[str],
        typer.Option(
            "--select",
            help="Aggregates, comma-separated: count(), sum/avg/min/max/pXX over "
            "cost, total_tokens, prompt_tokens, completion_tokens, latency, "
            "score[<name>]. E.g. 'avg(cost) AS avg_cost, count()'.",
        ),
    ] = None,
    where: Annotated[
        Optional[str],
        typer.Option(
            "--where",
            help="Filter predicates joined by AND, e.g. "
            "'asset = summarizer AND status = success'. Fields: asset, "
            "deployment_environment, variant, log_level, model, model_id, "
            "status, tag. Operators: = != < <= > >= IN (...).",
        ),
    ] = None,
    group_by: Annotated[
        Optional[str],
        typer.Option(
            "--group-by",
            help="Dimensions to group by, comma-separated (same allow-list as --where fields).",
        ),
    ] = None,
    since: Annotated[
        Optional[str],
        typer.Option(
            "--since",
            help="Start of the time window (inclusive): 7d, 24h, P30D, or an RFC 3339 timestamp.",
        ),
    ] = None,
    until: Annotated[
        Optional[str],
        typer.Option("--until", help="End of the time window (exclusive), RFC 3339; default now."),
    ] = None,
    limit: Annotated[
        Optional[int],
        typer.Option("--limit", help="Max result rows (default 100, ceiling 10000)."),
    ] = None,
    order_by: Annotated[
        Optional[str],
        typer.Option(
            "--order-by",
            help="Sort rows: '<alias> [asc|desc]' (default: first select, descending).",
        ),
    ] = None,
    query_file: Annotated[
        Optional[Path],
        typer.Option(
            "--query-file",
            help="JSON/YAML query object (the saveable form); flags override its fields.",
        ),
    ] = None,
    capsule_dir: Annotated[
        Optional[Path],
        typer.Option(
            "--capsule-dir",
            help="Capsule storage directory (defaults to $NOVAFABRIC_CAPSULE_DIR).",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json", help="Emit the canonical JSON result (the default table renders it)."
        ),
    ] = False,
    rebuild_index: Annotated[
        bool,
        typer.Option(
            "--rebuild-index",
            help="Force a fresh index build. (This slice rebuilds the derived in-memory "
            "index on every query, so this is currently always implied.)",
        ),
    ] = False,
) -> None:
    """Aggregate metrics across local Run Capsules, offline (experimental).

    A bounded, declarative filter/group-by/aggregate over the local capsule
    directory — read-only, no server, no network, no raw SQL (ADR-0129).

    Scope: all capsules in one local capsule directory.

    \b
    Examples:
      nova query --select 'count()'
      nova query --select 'avg(cost), count()' \\
        --where 'asset = summarizer AND deployment_environment = production' \\
        --group-by model --since 7d --json
      nova query --select 'p95(latency) AS p95_ms' --group-by status
      nova query --query-file q.yaml --capsule-dir ./capsules
    """
    del rebuild_index  # accepted per ADR-0129; index is rebuilt fresh each run
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

    base = (capsule_dir or default_capsule_dir()).resolve()
    try:
        result = run_query(plan, base)
    except QueryError as exc:
        typer.echo(f"Query error: {exc}", err=True)
        raise SystemExit(1) from exc

    if json_output:
        typer.echo(json.dumps(result))
        return

    typer.echo(_format_table(result["columns"], result["rows"]))
    summary = (
        f"\n{result['row_count']} row(s)"
        f"{' (truncated)' if result['truncated'] else ''} — "
        f"{result['index']['capsule_count']} capsule(s) scanned, "
        f"engine {result['index']['engine']}, window "
        f"{result['time_window']['since']} → {result['time_window']['until']}"
    )
    typer.echo(summary)
