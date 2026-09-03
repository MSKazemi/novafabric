"""nova assure-canary — the canary-run record (ADR-0147 D3, NF-153 evidence half).

Records one canary replay of a pinned baseline: which baseline, when, **which
stack**, the C3 equivalence verdict, its drift score, and whether that alarms.

⚠ **This records a canary run; it does not schedule or perform one.** NF-153 also
requires re-running each pinned baseline against the current stack on a declared
cadence. That orchestration needs live infrastructure and is **not built** —
ADR-0147's standing production loop still does not exist.

The verdict comes from C3 (`nova replay-equivalence check`); nothing here scores
equivalence. Exits `1` when the run alarms, so a driving loop can notice.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from novafabric.assure._honesty import HONESTY_LINE
from novafabric.assure.canary import CanaryError, record_canary_run

app = typer.Typer(
    name="assure-canary",
    help="Record a canary replay of a pinned baseline (experimental, ADR-0147 NF-153).",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)


@app.command("record")
def record(
    run_doc: Annotated[
        Path,
        typer.Option(
            "--run",
            help='JSON: {"baseline_id","ran_at","stack":{...},"equivalent":bool,'
            '"drift_score":float,"baseline_stack":{...}}',
        ),
    ],
    out: Annotated[
        Path | None, typer.Option("--out", help="Write the record JSON here.")
    ] = None,
) -> None:
    """Record one canary replay of a pinned baseline."""
    if not run_doc.is_file():
        err_console.print(f"[red]Run document not found:[/red] {run_doc}")
        raise typer.Exit(2)
    try:
        doc: Any = json.loads(run_doc.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        err_console.print(f"[red]Could not read the run document:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict):
        err_console.print("[red]Run document must be a JSON object.[/red]")
        raise typer.Exit(2)

    stack = doc.get("stack")
    if not isinstance(stack, dict):
        err_console.print("[red]Run document needs a 'stack' object.[/red]")
        raise typer.Exit(2)
    if not isinstance(doc.get("equivalent"), bool):
        err_console.print(
            "[red]Run document needs a boolean 'equivalent' — the C3 verdict.[/red]"
        )
        raise typer.Exit(2)
    baseline_stack = doc.get("baseline_stack")
    if baseline_stack is not None and not isinstance(baseline_stack, dict):
        err_console.print("[red]'baseline_stack' must be an object.[/red]")
        raise typer.Exit(2)

    try:
        run = record_canary_run(
            str(doc.get("baseline_id", "")),
            ran_at=str(doc.get("ran_at", "")),
            stack=stack,
            equivalent=doc["equivalent"],
            drift_score=doc.get("drift_score"),
            baseline_stack=baseline_stack,
        )
    except CanaryError as exc:
        err_console.print(f"[red]Cannot record the canary run:[/red] {exc}")
        raise typer.Exit(2) from exc

    payload = run.model_dump(exclude_none=True)
    if out is not None:
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        console.print(f"[green]Recorded[/green] {run.baseline_id} -> {out}")
    else:
        console.print_json(json.dumps(payload))

    if run.same_stack is False:
        console.print(
            "[yellow]stack changed since the baseline was pinned[/yellow] — a "
            "difference here may be the stack, not the agent."
        )
    elif run.same_stack is None:
        console.print(
            "[yellow]baseline stack unknown[/yellow] — this run was not confirmed "
            "to be a like-for-like comparison."
        )
    console.print(f"[dim]{HONESTY_LINE}[/dim]")

    if run.alarm:
        err_console.print(
            f"[red]ALARM:[/red] canary for {run.baseline_id} was not equivalent"
        )
        raise typer.Exit(1)
