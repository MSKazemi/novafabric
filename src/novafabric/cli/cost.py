"""CLI commands for LLM cost reporting (cap-002, ADR-0066; estimate: ADR-0133)."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Optional

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


# ---------------------------------------------------------------------------
# nova cost estimate — offline, per-capsule (ADR-0133, experimental)
# ---------------------------------------------------------------------------


def _format_estimate_table(rows: list[dict[str, Any]]) -> str:
    headers = ["model_id", "basis", "layer", "currency", "calls", "amount"]
    widths = {h: len(h) for h in headers}
    for row in rows:
        for h in headers:
            widths[h] = max(widths[h], len(str(row.get(h, ""))))
    sep = "  "
    lines = [
        sep.join(h.ljust(widths[h]) for h in headers),
        sep.join("-" * widths[h] for h in headers),
    ]
    lines.extend(
        sep.join(str(row.get(h, "")).ljust(widths[h]) for h in headers) for row in rows
    )
    return "\n".join(lines)


@app.command("estimate")
def cost_estimate_cmd(
    capsule_dir: Path = typer.Argument(
        ..., help="Capsule directory containing model-calls.jsonl."
    ),
    pricing_catalog: Optional[Path] = typer.Option(
        None,
        "--pricing-catalog",
        help="Explicit pricing catalog file (highest-precedence layer).",
    ),
    at: Optional[str] = typer.Option(
        None, "--at", help="Price as of DATE (YYYY-MM-DD); default: today."
    ),
    fmt: str = typer.Option("table", "--format", help="Output format: table | json"),
) -> None:
    """Offline cost for one capsule's model calls (ADR-0133, experimental).

    Fully local — no ClickHouse, no server, no network. Each call's recorded
    ``nova.cost`` is reported verbatim (basis=recorded; never overwritten or
    recomputed). Calls without a recorded cost are priced from the merged
    local pricing catalog and labeled basis=estimated; models absent from
    every catalog layer stay unpriced (cost 0.0, exactly as before).

    \b
    Examples:
      nova cost estimate ~/.novafabric/capsules/<run_id>
      nova cost estimate ./capsule --pricing-catalog ./pricing.yaml --format json
    """
    from novafabric.cost.pricing_catalog import (  # noqa: PLC0415 — keep CLI import light
        PricingCatalogError,
        cost_for_model_call_record,
        load_catalog_file,
        load_merged_catalog,
    )

    if fmt not in ("table", "json"):
        typer.echo(f"Error: unknown format {fmt!r}; choose table or json", err=True)
        raise SystemExit(1)
    at_date: date | None = None
    if at is not None:
        try:
            at_date = date.fromisoformat(at)
        except ValueError:
            typer.echo(f"Error: --at must be a YYYY-MM-DD date, got {at!r}", err=True)
            raise SystemExit(1) from None
    if not capsule_dir.is_dir():
        typer.echo(f"Error: capsule directory not found: {capsule_dir}", err=True)
        raise SystemExit(1)
    if pricing_catalog is not None:
        try:
            load_catalog_file(pricing_catalog)
        except PricingCatalogError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise SystemExit(1) from exc

    catalog = load_merged_catalog(explicit=pricing_catalog)
    for warning in catalog.warnings:
        typer.echo(f"Warning: {warning}", err=True)

    calls_path = capsule_dir / "model-calls.jsonl"
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    totals: dict[tuple[str, str], float] = {}
    call_count = 0
    if calls_path.is_file():
        for line in calls_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            call_count += 1
            model = record.get("gen_ai.response.model") or record.get(
                "gen_ai.request.model"
            )
            model_id = model if isinstance(model, str) and model else "(unknown)"
            cost = cost_for_model_call_record(record, catalog, at=at_date)
            if cost is None:
                key = (model_id, "unpriced", "", "")
                row = grouped.setdefault(
                    key,
                    {
                        "model_id": model_id,
                        "basis": "unpriced",
                        "layer": "",
                        "currency": "",
                        "calls": 0,
                        "amount": "",
                    },
                )
                row["calls"] += 1
                continue
            basis = cost["basis"]
            currency = cost["currency"]
            layer = cost.get("pricing_source_layer", "")
            key = (model_id, basis, currency, layer)
            row = grouped.setdefault(
                key,
                {
                    "model_id": model_id,
                    "basis": basis,
                    "layer": layer,
                    "currency": currency,
                    "calls": 0,
                    "amount": 0.0,
                },
            )
            row["calls"] += 1
            row["amount"] = round(row["amount"] + cost["amount"], 6)
            totals_key = (basis, currency)
            totals[totals_key] = round(
                totals.get(totals_key, 0.0) + cost["amount"], 6
            )

    rows = sorted(grouped.values(), key=lambda r: (r["model_id"], r["basis"]))
    if fmt == "json":
        payload = {
            "capsule_dir": str(capsule_dir),
            "calls": call_count,
            "pricing_catalog_digest": catalog.digest,
            "rows": rows,
            "totals": [
                {"basis": basis, "currency": currency, "amount": amount}
                for (basis, currency), amount in sorted(totals.items())
            ],
        }
        typer.echo(json.dumps(payload, indent=2))
        return
    if not rows:
        typer.echo("(no model calls)")
        return
    typer.echo(_format_estimate_table(rows))
    typer.echo("")
    for (basis, currency), amount in sorted(totals.items()):
        typer.echo(f"{basis} total: {amount:g} {currency}")
    typer.echo(
        "estimated amounts are derived from the local pricing catalog "
        f"({catalog.digest}) — estimates, not billing records"
    )
