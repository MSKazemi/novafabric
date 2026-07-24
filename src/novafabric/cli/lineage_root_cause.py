# src/novafabric/cli/lineage_root_cause.py
from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from novafabric.lineage._store import LineageStore
from novafabric.lineage.analytics.root_cause import (
    UnknownLineageRunError,
    rank_root_causes,
)

console = Console()


class RootCauseOutputFormat(str, Enum):
    text = "text"
    json = "json"


def root_cause_cmd(
    run_id: Annotated[
        str, typer.Argument(help="Run id whose upstream root cause to rank.")
    ],
    depth: Annotated[
        int, typer.Option("--depth", "-d", help="Max provenance depth.")
    ] = 5,
    capsule_dir: Annotated[
        Optional[Path],
        typer.Option(
            "--capsule-dir",
            help="Capsule storage dir for ADR-0084 step-level taxonomy "
            "(defaults to none: lineage payload cues only).",
        ),
    ] = None,
    output: Annotated[
        RootCauseOutputFormat,
        typer.Option("--output", "-o", help="Output format."),
    ] = RootCauseOutputFormat.text,
) -> None:
    """Rank upstream lineage nodes by root-cause likelihood for a failed run.

    Experimental (ADR-0213). Combines error signals, recency decay, edge
    confidence, and failure correlation across sibling failed runs. Scores are
    relative ranking weights, not calibrated probabilities; with no error
    signal anywhere the command honestly reports no responsible node.

    Scope: lineage graph (plus capsules when --capsule-dir is given).

    \b
    Examples:
      # Rank suspects for a failed run
      nova lineage root-cause 01HX...

      # Deeper walk, machine-readable
      nova lineage root-cause 01HX... --depth 8 --output json
    """
    store = LineageStore()
    try:
        report = rank_root_causes(
            store, run_id, depth=depth, capsule_dir=capsule_dir
        )
    except UnknownLineageRunError:
        console.print(f"[red]x[/red] Unknown run: {run_id}")
        raise typer.Exit(code=1) from None

    if output == RootCauseOutputFormat.json:
        typer.echo(json.dumps(report.as_dict(), indent=2))
        return

    if not report.suspects:
        console.print(f"[dim]No upstream provenance for {run_id}[/dim]")
        for note in report.notes:
            console.print(f"[dim]{note}[/dim]")
        return

    if report.responsible is None:
        console.print(
            f"[yellow]![/yellow] No error signal upstream of {run_id} — "
            "no responsible node."
        )
    else:
        console.print(
            f"[bold]Most likely root cause:[/bold] "
            f"[cyan]{report.responsible.kind}[/cyan]:"
            f"[yellow]{report.responsible.ref}[/yellow] "
            f"(taxonomy: {report.taxonomy.value})"
        )
    table = Table(title=f"Ranked suspects upstream of {run_id}")
    table.add_column("kind", style="cyan")
    table.add_column("ref", style="yellow")
    table.add_column("score", justify="right")
    table.add_column("signals")
    for s in report.suspects:
        table.add_row(s.kind, s.ref, f"{s.score:.4f}", "; ".join(s.signals) or "-")
    console.print(table)
    for note in report.notes:
        console.print(f"[dim]{note}[/dim]")
