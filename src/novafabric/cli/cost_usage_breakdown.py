"""nova cost usage-breakdown — token usage-type composition (ADR-0132 D3/D4).

Read-only. Loads a capsule manifest (or a bare ``usage_totals`` object) and prints the
**composition** of its token volume — each usage type's share of the counted tokens — plus the
cached-read ratio and factual `has_reasoning_tokens` / `is_multimodal` flags. It reports **token
composition only**: no cost/dollars (pricing is ADR-0133) and no efficient/within-budget/verdict
field. Honours the ADR-0132 "absent != zero" rule — a usage type never reported is absent from the
composition, never zero-filled.

This is the CLI half; it reads the ``usage_totals`` aggregate NovaFabric already writes to the
capsule manifest at finalize.

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


def cost_usage_breakdown_cmd(
    document: Annotated[
        Path,
        typer.Argument(
            help="A capsule manifest.json (reads its `usage_totals`) or a bare usage_totals object."
        ),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the breakdown as JSON."),
    ] = False,
) -> None:
    """Report the token usage-type composition of a capsule — descriptive only.

    \b
    Examples:
      nova cost usage-breakdown my-capsule/manifest.json
      nova cost usage-breakdown usage.json --json
    """
    from novafabric.cost.usage_breakdown import compute_usage_breakdown

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

    # Accept a full manifest (with a `usage_totals` key) or a bare usage_totals object.
    usage_totals = doc.get("usage_totals", doc)
    if not isinstance(usage_totals, dict):
        err_console.print("[red]`usage_totals` must be an object.[/red]")
        raise typer.Exit(2)

    breakdown = compute_usage_breakdown(usage_totals)

    if json_out:
        print(json.dumps(breakdown.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    console.print(
        f"Token usage breakdown (composition — not cost): "
        f"{breakdown.counted_tokens} counted token(s)"
    )
    for usage_type, share in breakdown.composition.items():
        console.print(f"  {usage_type}: {share * 100:.1f}%")
    if breakdown.cached_read_ratio is not None:
        console.print(f"  cached-read ratio: {breakdown.cached_read_ratio * 100:.1f}%")
    console.print(
        f"  reasoning: {breakdown.has_reasoning_tokens}  multimodal: {breakdown.is_multimodal}"
    )

    raise typer.Exit(0)
