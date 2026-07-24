"""nova eval cost — self-reported eval-cost / compute disclosure (ADR-0154 D2 / NF-229).

Read-only. Loads the self-reported cost/compute figures for an eval run and renders the disclosure:
``wall_seconds``, ``token_in``, ``token_out``, ``usd_cost``, and optionally ``energy_wh`` +
``hardware_ref``. Per NF-229 every value is **self-reported** — NovaFabric discloses what the
harness reported, it does not measure or certify these figures.

This first slice reads the figures from the document; the collector that reads ``facets.eval_cost``
from a sealed capsule (``--capsule <run_id>``) is a documented follow-on.

Exit codes: 0 — rendered; 2 — the input is missing/malformed, or a figure is negative.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

console = Console()
err_console = Console(stderr=True)


def eval_cost_cmd(
    document: Annotated[
        Path,
        typer.Argument(
            help="JSON: {wall_seconds, token_in, token_out, usd_cost, energy_wh?, hardware_ref?}."
        ),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the disclosure as JSON."),
    ] = False,
) -> None:
    """Render a self-reported eval-cost / compute disclosure.

    \b
    Examples:
      nova eval cost cost.json
      nova eval cost cost.json --json
    """
    from novafabric.eval.integrity.cost import build_eval_cost

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

    required = ("wall_seconds", "token_in", "token_out", "usd_cost")
    missing = [k for k in required if k not in doc]
    if missing:
        err_console.print(f"[red]Missing required field(s):[/red] {', '.join(missing)}")
        raise typer.Exit(2)

    try:
        record = build_eval_cost(
            wall_seconds=float(doc["wall_seconds"]),
            token_in=int(doc["token_in"]),
            token_out=int(doc["token_out"]),
            usd_cost=float(doc["usd_cost"]),
            energy_wh=None if doc.get("energy_wh") is None else float(doc["energy_wh"]),
            hardware_ref=doc.get("hardware_ref"),
        )
    except (ValueError, TypeError) as exc:
        err_console.print(f"[red]Invalid eval-cost input:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(record.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    # Normative per NF-221-230: every output carries the honesty line.
    console.print(f"[dim]{record.honesty_line}[/dim]")
    console.print("Eval-cost disclosure (self-reported — not a NovaFabric measurement)")
    console.print(f"  wall_seconds: {record.wall_seconds}")
    console.print(f"  tokens:       in={record.token_in} out={record.token_out}")
    console.print(f"  usd_cost:     {record.usd_cost}")
    console.print(f"  energy_wh:    {record.energy_wh}")
    console.print(f"  hardware_ref: {record.hardware_ref}")

    raise typer.Exit(0)
