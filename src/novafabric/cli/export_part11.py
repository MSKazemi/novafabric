"""nova export-part11 — 21 CFR Part 11 electronic-records evidence artifact (ADR-0160 / NF-282).

Read-only. Loads per-element source refs and renders the Part 11 electronic-records/signatures
elements a run recorded — signer identity, §11.50 signing intent, DSSE signature binding, record
integrity, trusted timestamp, audit trail — each ``complete`` / ``partial`` / ``missing``. It
**renders facts, never a conformity determination** — a qualified human makes the Part 11 call —
and, per ADR-0160, prints the binding medical-honesty banner in **every** output (rich and JSON).

This is the CLI half of NF-282; the collector that reads the elements from the sealed capsule (the
§11.50 signing intent, the DSSE seal, the RFC 3161 TSR when present) is a documented follow-on.

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
    "complete": "[green]●[/green]",
    "partial": "[yellow]◐[/yellow]",
    "missing": "[red]○[/red]",
}


def export_part11_cmd(
    document: Annotated[
        Path,
        typer.Argument(help="JSON: {capsule_root, elements:{name:ref}, partial:{name:reason}}."),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the Part 11 record as JSON."),
    ] = False,
) -> None:
    """Render a 21 CFR Part 11 electronic-records evidence artifact — facts, never a Part 11 call.

    \b
    Examples:
      nova export-part11 part11.json
      nova export-part11 part11.json --json
    """
    from novafabric.compliance.export.healthcare.part11 import build_part11_record

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
    capsule_root = doc.get("capsule_root")
    if not isinstance(capsule_root, str) or not capsule_root:
        err_console.print("[red]Document must carry a 'capsule_root' string.[/red]")
        raise typer.Exit(2)

    try:
        record = build_part11_record(
            capsule_root=capsule_root,
            elements=doc.get("elements", {}),
            partial=doc.get("partial"),
        )
    except (ValueError, TypeError) as exc:
        err_console.print(f"[red]Invalid Part 11 input:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(record.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    # The binding medical-honesty banner — ADR-0160: "in every artifact and CLI output".
    console.print(f"[dim]{record.banner}[/dim]")
    s = record.summary
    console.print(
        f"\n{record.standard}  (capsule {record.capsule_root[:12]}…)\n"
        f"complete={s['complete']} partial={s['partial']} missing={s['missing']}"
    )
    for field in record.fields:
        mark = _MARK.get(field.status, "?")
        detail = f"  {field.source_ref}" if field.source_ref else ""
        detail += f"  ({field.reason})" if field.reason else ""
        console.print(f"  {mark} {field.element:<20} {field.status}{detail}")

    raise typer.Exit(0)
