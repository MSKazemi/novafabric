"""nova export-public-disclosure — DRAFT public-sector disclosure record (ADR-0169 D1 / NF-373).

Read-only. Loads supplied references and renders a DRAFT public-sector agentic-AI disclosure record:
it **references, never re-authors or asserts** — ``authority_ref`` is a declared reference to the
public body (never a NovaFabric assertion), ``system_card_ref`` binds an E7 system card by digest,
``capsule_refs`` are digests of the sealed runs summarized. Required fields backed by nothing are
listed for manual completion, never fabricated. NovaFabric never registers/publishes/transmits — the
output is a DRAFT the public body submits.

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


def export_public_disclosure_cmd(
    document: Annotated[
        Path,
        typer.Argument(
            help="JSON: {authority_ref, agent_ref, decision_scope, human_oversight_ref, "
            "capsule_refs[], system_card_ref?}."
        ),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the DRAFT record as JSON."),
    ] = False,
) -> None:
    """Render a DRAFT public-sector disclosure record from supplied references.

    \b
    Examples:
      nova export-public-disclosure disclosure.json
      nova export-public-disclosure disclosure.json --json
    """
    from novafabric.compliance.export.public._public_sector import (
        build_public_sector_disclosure,
    )

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
        record = build_public_sector_disclosure(
            authority_ref=doc.get("authority_ref"),
            agent_ref=doc.get("agent_ref"),
            decision_scope=doc.get("decision_scope"),
            human_oversight_ref=doc.get("human_oversight_ref"),
            capsule_refs=doc.get("capsule_refs", []),
            system_card_ref=doc.get("system_card_ref"),
        )
    except (ValueError, TypeError) as exc:
        err_console.print(f"[red]Invalid document:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(record.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    # status in parens, not "[...]", which rich would parse as markup and strip.
    console.print(f"Public-sector disclosure ({record.status})")
    console.print(f"  authority_ref (declared):  {record.authority_ref}")
    console.print(f"  agent_ref:                 {record.agent_ref}")
    console.print(f"  decision_scope:            {record.decision_scope}")
    console.print(f"  human_oversight_ref:       {record.human_oversight_ref}")
    console.print(f"  system_card_ref (by digest): {record.system_card_ref}")
    console.print(f"  capsule_refs ({len(record.capsule_refs)}):")
    for ref in record.capsule_refs:
        console.print(f"    {ref}")
    if record.manual_completion_required:
        console.print(
            "  manual completion required (not fabricated): "
            + ", ".join(record.manual_completion_required)
        )

    raise typer.Exit(0)
