"""nova export-election-disclosure — content-provenance disclosure (ADR-0169 D5 / NF-379).

Read-only. Loads an item of AI-generated political/civic content's refs and renders a
content-provenance + agent-evidence disclosure: ``content_ref``, ``provenance_receipt_ref`` (binds
an NF-094 / C2PA / SynthID receipt by digest), ``disclosure_label`` (``ai_generated`` /
``ai_assisted`` / ``synthetic_media``), and ``capsule_refs``.

It records a *disclosure* and **adjudicates nothing** (I-4): it never determines whether the content
is lawful, deceptive, or election-regulated — the label states what was recorded about provenance,
never a legal conclusion.

Exit codes: 0 — rendered; 2 — the input is missing/malformed, or ``disclosure_label`` is invalid.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

console = Console()
err_console = Console(stderr=True)


def export_election_disclosure_cmd(
    document: Annotated[
        Path,
        typer.Argument(
            help="JSON: {content_ref, provenance_receipt_ref, disclosure_label, capsule_refs[]}."
        ),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the disclosure as JSON."),
    ] = False,
) -> None:
    """Render an election/democratic-process content-provenance disclosure — records, never judges.

    \b
    Examples:
      nova export-election-disclosure elec.json
      nova export-election-disclosure elec.json --json
    """
    from novafabric.compliance.export.public._election import build_election_disclosure

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
        disclosure = build_election_disclosure(
            content_ref=str(doc.get("content_ref") or ""),
            provenance_receipt_ref=str(doc.get("provenance_receipt_ref") or ""),
            disclosure_label=str(doc.get("disclosure_label") or ""),
            capsule_refs=doc.get("capsule_refs", []),
        )
    except (ValueError, TypeError) as exc:
        err_console.print(f"[red]Invalid document:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(disclosure.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    console.print("Election / democratic-process disclosure (records provenance — judges nothing)")
    console.print(f"  content_ref:            {disclosure.content_ref}")
    console.print(f"  provenance_receipt_ref: {disclosure.provenance_receipt_ref}")
    console.print(f"  disclosure_label:       {disclosure.disclosure_label}")
    console.print(f"  capsule_refs ({len(disclosure.capsule_refs)}):")
    for ref in disclosure.capsule_refs:
        console.print(f"    {ref}")

    raise typer.Exit(0)
