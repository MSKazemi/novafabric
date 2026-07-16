"""nova export-rai-scorecard — Responsible-AI coverage scorecard (ADR-0158 D4 / NF-262).

Read-only. Loads per-dimension evidence refs and prints the RAI scorecard: each dimension marked
``supported`` / ``partial`` / ``unsupported`` / ``not_applicable`` by presence of evidence. It is
**coverage, never a score** — no threshold, no fair/unfair or pass/fail label (ADR-0158 I-4).

This is the CLI half of NF-262; the collector that discovers the per-dimension evidence refs from
sealed evidence is a documented follow-on.

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

_MARK = {
    "supported": "[green]●[/green]",
    "partial": "[yellow]◐[/yellow]",
    "unsupported": "[red]○[/red]",
    "not_applicable": "[dim]—[/dim]",
}


def export_rai_scorecard_cmd(
    document: Annotated[
        Path,
        typer.Argument(help="JSON: {evidence:{dimension:[refs]}, not_applicable[], partial[]}."),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the scorecard as JSON."),
    ] = False,
) -> None:
    """Render a Responsible-AI coverage scorecard — presence of evidence per cell, never a score.

    \b
    Examples:
      nova export-rai-scorecard rai.json
      nova export-rai-scorecard rai.json --json
    """
    from novafabric.compliance.rai.scorecard import build_rai_scorecard

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

    try:
        scorecard = build_rai_scorecard(
            doc.get("evidence", {}),
            not_applicable=doc.get("not_applicable", []),
            partial=doc.get("partial", []),
        )
    except (ValueError, TypeError) as exc:
        err_console.print(f"[red]Invalid scorecard input:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(scorecard.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    s = scorecard.summary
    console.print(
        "RAI scorecard (coverage, not a score):  "
        f"supported={s['supported']} partial={s['partial']} "
        f"unsupported={s['unsupported']} n/a={s['not_applicable']}"
    )
    for cell in scorecard.cells:
        mark = _MARK.get(cell.coverage.value, "?")
        refs = f"  {', '.join(cell.evidence_refs)}" if cell.evidence_refs else ""
        console.print(f"  {mark} {cell.dimension:<20} {cell.coverage.value}{refs}")

    raise typer.Exit(0)
