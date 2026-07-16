"""nova export-public-annex-viii — EU AI Act Annex VIII DRAFT public-DB entry (ADR-0169 D1/NF-371).

Read-only. Loads per-field sources and renders the DRAFT Annex VIII / Art. 71 public-database entry:
each required field is ``capsule_evidence`` (a digest/ref into the sealed capsule — never the raw
value) or ``operator_declared`` (the operator's public declaration); required fields backed by
neither are listed as unmapped, never fabricated. NovaFabric never registers/publishes/transmits —
the output is a DRAFT the operator submits.

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


def export_public_annex_viii_cmd(
    document: Annotated[
        Path,
        typer.Argument(help="JSON: {capsule_root, capsule_evidence{}, operator_declared{}}."),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the DRAFT entry as JSON."),
    ] = False,
) -> None:
    """Render a DRAFT EU AI Act Annex VIII public-DB entry from sealed evidence + declarations.

    \b
    Examples:
      nova export-public-annex-viii entry.json
      nova export-public-annex-viii entry.json --json
    """
    from novafabric.compliance.export.public.annex_viii import build_annex_viii_entry

    if not document.exists():
        err_console.print(f"[red]Document not found:[/red] {document}")
        raise typer.Exit(2)
    try:
        doc = json.loads(document.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        err_console.print(f"[red]Could not read document:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict) or "capsule_root" not in doc:
        err_console.print("[red]Document must be a JSON object with 'capsule_root'.[/red]")
        raise typer.Exit(2)

    try:
        entry = build_annex_viii_entry(
            capsule_root=str(doc["capsule_root"]),
            operator_declared=doc.get("operator_declared", {}),
            capsule_evidence=doc.get("capsule_evidence", {}),
        )
    except (ValueError, TypeError) as exc:
        err_console.print(f"[red]Invalid document:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(entry.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    # status in parens, not "[...]", which rich would parse as markup and strip.
    console.print(
        f"Annex VIII entry ({entry.status})  ({entry.standard})\n"
        f"  capsule_root: {entry.capsule_root}"
    )
    for f in entry.fields:
        detail = f.evidence_ref if f.source.value == "capsule_evidence" else f.value
        console.print(f"  {f.name:<26} {f.source.value}: {detail}")
    if entry.unmapped_required:
        console.print(
            "  unmapped (required, not fabricated): " + ", ".join(entry.unmapped_required)
        )

    raise typer.Exit(0)
