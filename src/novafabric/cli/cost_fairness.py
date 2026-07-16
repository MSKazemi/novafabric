"""nova cost fairness — agent cost/energy fairness ledger (ADR-0146 D5 / NF-150).

Read-only. Loads per-agent resource totals per dimension (cost / energy / calls) and prints the
fairness statistic — each agent's share, the Gini coefficient, and the max/mean ratio. It is
**descriptive evidence, never a verdict**: no threshold, quota, or pass/fail.

This is the CLI half of NF-150; the collector that derives the per-agent totals from the records a
capsule already holds (the ``cost`` interceptor + ``energy`` attribution) is a documented follow-on.

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


def cost_fairness_cmd(
    totals: Annotated[
        Path,
        typer.Argument(help="JSON: {totals: {dimension: {agent: total}}} (cost/energy/calls)."),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the fairness report as JSON."),
    ] = False,
) -> None:
    """Report per-agent fairness (share, Gini, max/mean) over resource totals — descriptive only.

    \b
    Examples:
      nova cost fairness totals.json
      nova cost fairness totals.json --json
    """
    from novafabric.cost.fairness import build_fairness_report

    if not totals.exists():
        err_console.print(f"[red]Totals document not found:[/red] {totals}")
        raise typer.Exit(2)
    try:
        doc = json.loads(totals.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        err_console.print(f"[red]Could not read totals document:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict) or "totals" not in doc or not isinstance(doc["totals"], dict):
        err_console.print("[red]Document must be a JSON object with a 'totals' map.[/red]")
        raise typer.Exit(2)

    try:
        report = build_fairness_report(doc["totals"])
    except (ValueError, TypeError) as exc:
        err_console.print(f"[red]Invalid totals:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(report.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    console.print("Fairness report (descriptive — not a quota):")
    for m in report.metrics:
        console.print(
            f"  {m.dimension}: gini={m.gini:.3f}  max/mean={m.max_mean_ratio:.2f}"
        )
        for agent, share in m.shares.items():
            console.print(f"    {agent}: {share * 100:.1f}%")

    raise typer.Exit(0)
