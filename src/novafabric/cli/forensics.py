"""nova forensics — read-only DFIR views over an incident's sealed evidence (ADR-0155).

This slice ships ``forensics timeline`` (D1): a deterministic, byte-identical-on-re-run view over
an incident's collected evidence. Future subcommands (``custody``, ``blast-radius`` — D2/D4) attach
here.

The command reconstructs only over already-sealed evidence supplied as a JSON document; missing
evidence is shown as ``gaps``, never raised (fail-open). It prints references and states only.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

forensics_app = typer.Typer(help="Read-only incident forensics over sealed evidence (ADR-0155).")
console = Console()
err_console = Console(stderr=True)


@forensics_app.command("timeline")
def timeline_cmd(
    evidence: Annotated[
        Path,
        typer.Argument(help="JSON: {incident_id, events:[{ts,source_capsule,seq,kind}], gaps?}."),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the timeline as JSON."),
    ] = False,
) -> None:
    """Render an incident's forensic timeline: a deterministic view over sealed evidence.

    \b
    Examples:
      nova forensics timeline incident-evidence.json
      nova forensics timeline incident-evidence.json --json
    """
    from novafabric.forensics.timeline import merge_timeline

    if not evidence.exists():
        err_console.print(f"[red]Evidence document not found:[/red] {evidence}")
        raise typer.Exit(2)
    try:
        doc = json.loads(evidence.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        err_console.print(f"[red]Could not read evidence document:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict) or "incident_id" not in doc:
        err_console.print("[red]Document must be a JSON object with 'incident_id'.[/red]")
        raise typer.Exit(2)

    try:
        timeline = merge_timeline(
            str(doc["incident_id"]), doc.get("events", []), gaps=doc.get("gaps", [])
        )
    except (KeyError, ValueError, TypeError) as exc:
        err_console.print(f"[red]Invalid evidence record:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(timeline.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    # Normative per NF-231-240: every exported artifact carries the line.
    console.print(f"[dim]{timeline.honesty_line}[/dim]")
    console.print(
        f"Forensic timeline: incident {timeline.incident_id}  "
        f"({len(timeline.events)} event(s), {len(timeline.gaps)} gap(s))"
    )
    for ev in timeline.events:
        detail = f"  {ev.detail}" if ev.detail else ""
        # kind rendered in parens, not "[...]", which rich would parse as markup and strip.
        console.print(f"  {ev.ts}  {ev.source_capsule}#{ev.seq}  ({ev.kind}){detail}")
    for gap in timeline.gaps:
        console.print(f"  [yellow]gap:[/yellow] {gap} (evidence not reconstructable)")

    raise typer.Exit(0)
