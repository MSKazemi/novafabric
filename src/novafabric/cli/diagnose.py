# src/novafabric/cli/diagnose.py
from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from novafabric._paths import default_capsule_dir
from novafabric.diagnose import CapsuleNotFoundError, attribute_failure

console = Console()


class DiagnoseOutputFormat(str, Enum):
    text = "text"
    json = "json"


def diagnose_cmd(
    run_id: Annotated[
        str, typer.Argument(help="Run id of the failed capsule to diagnose.")
    ],
    capsule_dir: Annotated[
        Optional[Path],
        typer.Option(
            "--capsule-dir",
            help="Capsule storage directory (defaults to $NOVAFABRIC_CAPSULE_DIR).",
        ),
    ] = None,
    output: Annotated[
        DiagnoseOutputFormat,
        typer.Option("--output", "-o", help="Output format."),
    ] = DiagnoseOutputFormat.text,
) -> None:
    """Attribute a failed run to its most likely responsible step (ADR-0084).

    Walks the captured trace plus the lineage / causal graph and produces a
    ranked attribution: which agent/step is most likely responsible, plus an
    AgentErrorTaxonomy label (MEMORY / REFLECTION / PLANNING / ACTION / SYSTEM /
    UNKNOWN). Scores are relative ranking weights, not probabilities.

    Scope: single failed run capsule.

    \b
    Examples:
      # Diagnose a failed run in the default capsule directory
      nova diagnose run-2026-06-11-abc123

      # Diagnose a capsule stored elsewhere, as JSON
      nova diagnose run-xyz --capsule-dir ./capsules --output json
    """
    base = (capsule_dir or default_capsule_dir()).resolve()
    cap = base / run_id

    # Best-effort lineage store for the causal-depth bias; absent/empty is fine.
    lineage_store = None
    try:
        from novafabric.lineage._store import LineageStore

        lineage_store = LineageStore()
    except Exception:
        lineage_store = None

    try:
        result = attribute_failure(cap, lineage_store=lineage_store)
    except CapsuleNotFoundError:
        console.print(
            f"[red]No capsule found for run id '{run_id}' under {base}[/red]"
        )
        raise typer.Exit(code=1)

    if output is DiagnoseOutputFormat.json:
        console.print_json(json.dumps(result.as_dict()))
        return

    head = f"[bold]Failure attribution for[/bold] {result.run_id} " \
        f"([yellow]{result.status}[/yellow])"
    console.print(head)
    if result.responsible is None:
        console.print(
            f"[yellow]Taxonomy:[/yellow] {result.taxonomy.value} — {result.note}"
        )
        return

    console.print(
        f"[bold]Most likely responsible:[/bold] "
        f"[red]{result.responsible.step_id}[/red] "
        f"({result.responsible.name}) — "
        f"[magenta]{result.taxonomy.value}[/magenta]"
    )

    table = Table(title="Ranked step attribution")
    table.add_column("rank", justify="right")
    table.add_column("step_id")
    table.add_column("name")
    table.add_column("kind")
    table.add_column("score", justify="right")
    table.add_column("taxonomy")
    for i, step in enumerate(result.ranked):
        table.add_row(
            str(i),
            step.step_id,
            step.name,
            step.kind,
            f"{step.score:.2f}",
            step.taxonomy.value,
        )
    console.print(table)
    console.print(f"[dim]{result.note}[/dim]")
