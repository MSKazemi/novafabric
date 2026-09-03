"""nova assure-impact — model-update impact report (ADR-0147 D3, NF-154).

Aggregates per-run C3 equivalence verdicts for a corpus of pinned baselines
replayed through a substituted model, into one report for ``from_model`` →
``to_model``.

**It decides nothing.** ADR-0147 states NF-154 *must not* decide whether to adopt
the new model, so the report carries no recommendation and the command always exits
``0`` on a successful aggregation — regressions included. Exiting non-zero on a
regression would be the adoption decision, made by an exit code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from novafabric.assure._honesty import HONESTY_LINE
from novafabric.assure.impact import (
    DEFAULT_WORST_N,
    ImpactError,
    RunOutcome,
    build_report,
)

app = typer.Typer(
    name="assure-impact",
    help="Model-update impact report over C3 verdicts (experimental, ADR-0147 NF-154).",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)


@app.command("report")
def report(
    corpus: Annotated[
        Path,
        typer.Option(
            "--corpus",
            help='JSON: {"from_model": "...", "to_model": "...", "runs": [...]}',
        ),
    ],
    worst_n: Annotated[
        int, typer.Option("--worst", help="How many regressions to list.")
    ] = DEFAULT_WORST_N,
    out: Annotated[
        Path | None, typer.Option("--out", help="Write the report JSON here.")
    ] = None,
) -> None:
    """Aggregate per-run equivalence verdicts into an impact report."""
    if not corpus.is_file():
        err_console.print(f"[red]Corpus not found:[/red] {corpus}")
        raise typer.Exit(2)
    try:
        doc: Any = json.loads(corpus.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        err_console.print(f"[red]Could not read corpus:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict):
        err_console.print("[red]Corpus must be a JSON object.[/red]")
        raise typer.Exit(2)

    for field in ("from_model", "to_model"):
        if not doc.get(field):
            err_console.print(f"[red]Corpus is missing {field!r}.[/red]")
            raise typer.Exit(2)
    runs = doc.get("runs")
    if not isinstance(runs, list):
        err_console.print("[red]Corpus 'runs' must be an array.[/red]")
        raise typer.Exit(2)

    try:
        outcomes = [RunOutcome.model_validate(r) for r in runs]
        built = build_report(
            outcomes,
            from_model=str(doc["from_model"]),
            to_model=str(doc["to_model"]),
            worst_n=worst_n,
        )
    except (ImpactError, ValueError) as exc:
        err_console.print(f"[red]Cannot build the impact report:[/red] {exc}")
        raise typer.Exit(2) from exc

    payload = built.model_dump(exclude_none=True)
    if out is not None:
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        console.print(f"[green]Report[/green] -> {out}")
    else:
        console.print_json(json.dumps(payload))

    if built.inconclusive:
        console.print(
            f"[yellow]{built.inconclusive} of {built.n} run(s) were inconclusive[/yellow]"
            " — not counted as equivalent."
        )
    for delta, label in ((built.cost_delta, "cost"), (built.token_delta, "token")):
        if delta.missing_runs:
            console.print(
                f"[yellow]{label} delta covers {delta.contributing_runs} of "
                f"{built.n} run(s)[/yellow] — {delta.missing_runs} carried no data."
            )
    console.print(f"[dim]{HONESTY_LINE}[/dim]")
