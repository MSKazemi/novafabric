# src/novafabric/cli/insights.py
from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from novafabric.lineage._store import LineageGraphTooLargeError, LineageStore
from novafabric.lineage.analytics.insights import (
    InsightsReport,
    build_insights_report,
)

console = Console()


class InsightsOutputFormat(str, Enum):
    table = "table"
    json = "json"
    markdown = "markdown"


def insights_cmd(
    top: Annotated[
        int, typer.Option("--top", help="How many hub nodes to include.")
    ] = 10,
    output: Annotated[
        InsightsOutputFormat,
        typer.Option("--output", help="Output format."),
    ] = InsightsOutputFormat.table,
    out_file: Annotated[
        Optional[Path],
        typer.Option(
            "--out", "-o",
            help="Write to a file instead of stdout "
            "(table output falls back to markdown in files).",
        ),
    ] = None,
    cost_db: Annotated[
        Optional[Path],
        typer.Option(
            "--cost-db",
            help="Optional evidence-fabric DuckDB accumulator to aggregate "
            "cost hotspots from (best-effort).",
        ),
    ] = None,
) -> None:
    """Synthesize the captured lineage graph into one intelligence report.

    Experimental (ADR-0215). Hubs and single points of failure (ADR-0212),
    seeded Louvain communities, orphan nodes, graph health, and best-effort
    cost hotspots. Unavailable data sources are reported as unavailable,
    never fabricated.

    Scope: whole lineage graph (local store).

    \b
    Examples:
      # Terminal report
      nova insights

      # Weekly artifact for a review ticket
      nova insights --output markdown -o insights.md

      # Machine-readable
      nova insights --output json
    """
    try:
        report = build_insights_report(
            LineageStore(), top_n=top, cost_db=cost_db
        )
    except LineageGraphTooLargeError as exc:
        console.print(f"[red]x[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if out_file is not None:
        rendered = (
            json.dumps(report.as_dict(), indent=2) + "\n"
            if output == InsightsOutputFormat.json
            else report.to_markdown()
        )
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(rendered, encoding="utf-8")
        console.print(f"[green]✓[/green] insights written to {out_file}")
        return

    if output == InsightsOutputFormat.json:
        typer.echo(json.dumps(report.as_dict(), indent=2))
        return
    if output == InsightsOutputFormat.markdown:
        typer.echo(report.to_markdown())
        return
    _render_table(report)


def _render_table(report: InsightsReport) -> None:
    health = report.health
    if health["node_count"] == 0:
        console.print("[dim]Lineage graph is empty — nothing to report.[/dim]")
        return
    console.print(
        f"[bold]Graph health:[/bold] {health['node_count']} nodes, "
        f"{health['edge_count']} edges, "
        f"largest component {health['largest_component_fraction']:.0%}, "
        f"orphans {report.orphan_total} ({health['orphan_ratio']:.0%})"
    )
    table = Table(title="Top hubs")
    table.add_column("kind", style="cyan")
    table.add_column("ref", style="yellow")
    table.add_column("in", justify="right")
    table.add_column("out", justify="right")
    table.add_column("SPOF", justify="center")
    for m in report.top_hubs:
        table.add_row(
            m.kind, m.ref, str(m.degree_in), str(m.degree_out),
            "!" if m.is_articulation_point else "",
        )
    console.print(table)
    if report.communities:
        console.print(
            f"[bold]Communities (size ≥ 2):[/bold] {len(report.communities)}"
        )
        for i, members in enumerate(report.communities):
            preview = ", ".join(members[:4]) + (" …" if len(members) > 4 else "")
            console.print(f"  [cyan]{i}[/cyan] ({len(members)}): {preview}")
    if report.cost_hotspots:
        console.print("[bold]Cost hotspots:[/bold]")
        for h in report.cost_hotspots:
            label = h.get("ref") or h.get("model", "?")
            cost = h.get("cost_usd")
            detail = (
                f"{cost:.4f} USD" if isinstance(cost, float)
                else f"{h.get('estimated_tokens', '?')} tokens"
            )
            console.print(f"  {label}: {detail}")
    else:
        console.print(f"[dim]Cost: {report.cost_note}[/dim]")
    console.print(f"[dim]{report.note}[/dim]")
