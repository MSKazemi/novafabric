"""nova export-transparency-register — DRAFT algorithm-register crosswalk (ADR-0169 D1 / NF-372).

Read-only. Loads per-field sources and renders a DRAFT algorithm-register record for the
``--standard``-selected register (UK ATRS, or the Amsterdam / Helsinki algorithm registers): each
required field is ``capsule_evidence`` (a digest/ref into the sealed capsule — never the raw value)
or ``operator_declared``; required fields backed by neither are listed for manual completion, never
fabricated. NovaFabric never registers/publishes/transmits — the output is a DRAFT the operator
submits, and the register standard is version-pinned.

Exit codes: 0 — rendered; 2 — the input is missing/malformed, or the standard is unknown.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

console = Console()
err_console = Console(stderr=True)


def export_transparency_register_cmd(
    document: Annotated[
        Path,
        typer.Argument(help="JSON: {capsule_root, capsule_evidence{}, operator_declared{}}."),
    ],
    standard: Annotated[
        str,
        typer.Option("--standard", help="Register shape: atrs | amsterdam | helsinki."),
    ] = "atrs",
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the DRAFT record as JSON."),
    ] = False,
) -> None:
    """Render a DRAFT algorithm-register record from sealed evidence + operator declarations.

    \b
    Examples:
      nova export-transparency-register reg.json --standard atrs
      nova export-transparency-register reg.json --standard amsterdam --json
    """
    from novafabric.compliance.export.public._transparency_register import (
        build_transparency_register,
    )

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
        record = build_transparency_register(
            standard=standard,
            capsule_root=str(doc["capsule_root"]),
            operator_declared=doc.get("operator_declared", {}),
            capsule_evidence=doc.get("capsule_evidence", {}),
        )
    except (ValueError, TypeError) as exc:
        err_console.print(f"[red]Invalid document:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(record.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    # status/standard in parens, not "[...]", which rich would parse as markup and strip.
    console.print(
        f"Transparency register ({record.status})  standard ({record.standard})  "
        f"({record.standard_version})\n  capsule_root: {record.capsule_root}"
    )
    for f in record.fields:
        detail = f.evidence_ref if f.source.value == "capsule_evidence" else f.value
        console.print(f"  {f.name:<28} {f.source.value}: {detail}")
    if record.manual_completion_required:
        console.print(
            "  manual completion required (not fabricated): "
            + ", ".join(record.manual_completion_required)
        )

    raise typer.Exit(0)
