# src/novafabric/cli/lineage_metrics.py
from __future__ import annotations

import json
from enum import Enum
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from novafabric.lineage._store import LineageGraphTooLargeError, LineageStore
from novafabric.lineage.analytics.centrality import compute_graph_metrics

console = Console()


class MetricsOutputFormat(str, Enum):
    text = "text"
    json = "json"


def metrics_cmd(
    top: Annotated[
        int, typer.Option("--top", help="How many hub nodes to list.")
    ] = 20,
    output: Annotated[
        MetricsOutputFormat,
        typer.Option("--output", "-o", help="Output format."),
    ] = MetricsOutputFormat.text,
) -> None:
    """Rank structurally critical lineage nodes: hubs and single points of failure.

    Experimental (ADR-0212). Computes degree, PageRank, betweenness, and
    articulation points over the local lineage graph. Scores are descriptive
    rankings for attention, not calibrated importance.

    Scope: whole lineage graph (local store).

    \b
    Examples:
      # Top hubs and articulation points as a table
      nova lineage metrics

      # Machine-readable, top 10 only
      nova lineage metrics --top 10 --output json
    """
    try:
        report = compute_graph_metrics(LineageStore(), top_n=top)
    except LineageGraphTooLargeError as exc:
        console.print(f"[red]x[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if output == MetricsOutputFormat.json:
        typer.echo(json.dumps(report.as_dict(), indent=2))
        return

    if report.node_count == 0:
        console.print("[dim]Lineage graph is empty — nothing to rank.[/dim]")
        return

    console.print(
        f"[bold]Lineage graph:[/bold] {report.node_count} nodes, "
        f"{report.edge_count} edges"
        + (" [dim](betweenness sampled)[/dim]" if report.sampled else "")
    )
    table = Table(title=f"Top {len(report.top_hubs)} hubs")
    table.add_column("kind", style="cyan")
    table.add_column("ref", style="yellow")
    table.add_column("in", justify="right")
    table.add_column("out", justify="right")
    table.add_column("pagerank", justify="right")
    table.add_column("betweenness", justify="right")
    table.add_column("SPOF", justify="center")
    for m in report.top_hubs:
        table.add_row(
            m.kind,
            m.ref,
            str(m.degree_in),
            str(m.degree_out),
            f"{m.pagerank:.4f}",
            f"{m.betweenness:.4f}",
            "!" if m.is_articulation_point else "",
        )
    console.print(table)
    if report.articulation_points:
        console.print(
            "[bold]Articulation points (single points of failure):[/bold] "
            + ", ".join(
                f"{m.kind}:{m.ref}" for m in report.articulation_points
            )
        )
    console.print(f"[dim]{report.note}[/dim]")
