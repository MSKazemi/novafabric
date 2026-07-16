"""nova cost attribute — wasted / failure-spend attribution (ADR-0146 D3 / NF-148).

Read-only. Loads per-run ``{run_id, status, cost}`` records and splits the recorded spend into
productive (a productive terminal status) vs wasted (everything else), with a per-status breakdown.
It is **descriptive evidence, never a verdict**: no threshold, quota, or over-budget field —
NovaFabric attributes spend to outcomes; whether that spend was acceptable is the operator's call.

This is the CLI half of NF-148; the collector that derives per-run cost + status from the records a
capsule already holds (the ``cost`` interceptor) is a documented follow-on.

Exit codes: 0 — rendered; 2 — the input is missing or malformed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

console = Console()
err_console = Console(stderr=True)


def cost_attribute_cmd(
    document: Annotated[
        Path,
        typer.Argument(
            help="JSON: {runs:[{run_id,status,cost}], productive_statuses?}."
        ),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the attribution as JSON."),
    ] = False,
) -> None:
    """Attribute recorded run cost to productive vs wasted outcomes — descriptive only.

    \b
    Examples:
      nova cost attribute runs.json
      nova cost attribute runs.json --json
    """
    from novafabric.cost.spend_attribution import attribute_spend

    if not document.exists():
        err_console.print(f"[red]Document not found:[/red] {document}")
        raise typer.Exit(2)
    try:
        doc = json.loads(document.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        err_console.print(f"[red]Could not read document:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict):
        err_console.print("[red]Document must be a JSON object.[/red]")
        raise typer.Exit(2)

    runs = doc.get("runs")
    if not isinstance(runs, list):
        err_console.print("[red]`runs` must be an array.[/red]")
        raise typer.Exit(2)
    productive_statuses = doc.get("productive_statuses", ["success"])
    if not isinstance(productive_statuses, list):
        err_console.print("[red]`productive_statuses` must be an array.[/red]")
        raise typer.Exit(2)

    try:
        report = attribute_spend(
            runs,
            productive_statuses=[str(s) for s in productive_statuses],
        )
    except (ValueError, TypeError) as exc:
        err_console.print(f"[red]Invalid spend input:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(report.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    console.print("Spend attribution (descriptive — not a budget verdict):")
    console.print(
        f"  total={report.total_spend:.4f}  productive={report.productive_spend:.4f}  "
        f"wasted={report.wasted_spend:.4f}  ({report.wasted_fraction * 100:.1f}% wasted)"
    )
    for status, spend in report.by_status.items():
        console.print(f"    {status}: {spend:.4f}")

    raise typer.Exit(0)
