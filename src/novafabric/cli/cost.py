"""CLI commands for LLM cost reporting (cap-002, ADR-0066)."""

from __future__ import annotations

import json
import os
from typing import Any

import typer

app = typer.Typer(
    help="Report LLM cost attribution per run.",
    no_args_is_help=True,
)


def _format_table(rows: list[dict[str, Any]]) -> str:
    """Render cost report rows as a plain-text table."""
    if not rows:
        return "(no data)"

    headers = ["date", "model_id", "total_usd", "prompt_tokens", "completion_tokens", "run_count"]
    col_widths = {h: len(h) for h in headers}
    for row in rows:
        for h in headers:
            col_widths[h] = max(col_widths[h], len(str(row.get(h, ""))))

    sep = "  "
    header_line = sep.join(h.ljust(col_widths[h]) for h in headers)
    divider = sep.join("-" * col_widths[h] for h in headers)
    lines = [header_line, divider]
    for row in rows:
        lines.append(
            sep.join(str(row.get(h, "")).ljust(col_widths[h]) for h in headers)
        )
    return "\n".join(lines)


def _format_csv(rows: list[dict[str, Any]]) -> str:
    """Render cost report rows as CSV."""
    if not rows:
        return ""
    headers = ["date", "model_id", "total_usd", "prompt_tokens", "completion_tokens", "run_count"]
    lines = [",".join(headers)]
    for row in rows:
        lines.append(",".join(str(row.get(h, "")) for h in headers))
    return "\n".join(lines)


@app.command("report")
def cost_report_cmd(
    tenant: str = typer.Option("default", "--tenant", help="Tenant identifier"),
    since_days: int = typer.Option(30, "--since-days", help="Days to look back"),
    fmt: str = typer.Option(
        "table",
        "--format",
        help="Output format: table | json | csv",
    ),
) -> None:
    """Generate a per-run LLM cost attribution report.

    Breaks down token usage and estimated cost by model, run, and asset.
    Requires NOVA_CLICKHOUSE_URL to be set.

    Scope: single capsule or registry-wide.

    \b
    Examples:
      nova cost report
      nova cost report --tenant acme --since-days 7
      nova cost report --format json | jq .
      nova cost report --format csv > costs.csv
    """
    url = os.environ.get("NOVA_CLICKHOUSE_URL")
    if not url:
        typer.echo(
            "Error: NOVA_CLICKHOUSE_URL not set. "
            "Cost reporting requires ClickHouse. "
            "Set NOVA_CLICKHOUSE_URL=http://user:pass@host:8123/nova",
            err=True,
        )
        raise SystemExit(1)

    if fmt not in ("table", "json", "csv"):
        typer.echo(f"Error: unknown format {fmt!r}; choose table, json, or csv", err=True)
        raise SystemExit(1)

    try:
        from novafabric.cost.clickhouse_store import (  # noqa: PLC0415
            query_cost_report,
        )

        rows = query_cost_report(tenant_id=tenant, since_days=since_days)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc
    except Exception as exc:
        typer.echo(f"ClickHouse query failed: {exc}", err=True)
        raise SystemExit(1) from exc

    if fmt == "json":
        typer.echo(json.dumps(rows, default=str))
    elif fmt == "csv":
        typer.echo(_format_csv(rows))
    else:
        if not rows:
            typer.echo(f"No cost data for tenant={tenant!r} in the last {since_days} days.")
        else:
            typer.echo(f"Cost report — tenant={tenant!r}, last {since_days} days\n")
            typer.echo(_format_table(rows))
