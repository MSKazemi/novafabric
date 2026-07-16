"""nova export-foia — DRAFT FOIA/public-records decision export (ADR-0169 D1 / NF-374).

Read-only. Loads a decision's ordered record index and redaction claims and renders a DRAFT
public-records export: an ordered ``record_index`` of included capsule/artifact digests,
``redactions`` (each a salted digest + the **claimed** statutory ``exemption_ref`` — never
NovaFabric's judgment; the withheld bytes are absent), and a deterministic ``custody_digest``
chaining the export to the sealed decision. Output is always DRAFT (NovaFabric never files it).

The selective-disclosure *prove-without-revealing* crypto (NF-376) is gated on ADR-0151 and is not
part of this export — this is the redaction-aware record, not a cryptographic disclosure proof.

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


def export_foia_cmd(
    document: Annotated[
        Path,
        typer.Argument(
            help="JSON: {decision_ref, record_index[], redactions[{digest, exemption_ref}]}."
        ),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the DRAFT export as JSON."),
    ] = False,
) -> None:
    """Render a DRAFT FOIA/public-records decision export from a record + redaction claims.

    \b
    Examples:
      nova export-foia foia.json
      nova export-foia foia.json --json
    """
    from novafabric.compliance.export.public._foia import build_foia_export

    if not document.exists():
        err_console.print(f"[red]Document not found:[/red] {document}")
        raise typer.Exit(2)
    try:
        doc = json.loads(document.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        err_console.print(f"[red]Could not read document:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict) or "decision_ref" not in doc:
        err_console.print("[red]Document must be a JSON object with 'decision_ref'.[/red]")
        raise typer.Exit(2)

    try:
        export = build_foia_export(
            decision_ref=str(doc["decision_ref"]),
            record_index=doc.get("record_index", []),
            redactions=doc.get("redactions", []),
        )
    except (ValueError, TypeError) as exc:
        err_console.print(f"[red]Invalid document:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(export.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    # status in parens, not "[...]", which rich would parse as markup and strip.
    console.print(f"FOIA / public-records export ({export.status})")
    console.print(f"  decision_ref:  {export.decision_ref}")
    console.print(f"  custody_digest: {export.custody_digest}")
    console.print(f"  record_index ({len(export.record_index)}, ordered):")
    for i, ref in enumerate(export.record_index):
        console.print(f"    {i}. {ref}")
    console.print(f"  redactions ({len(export.redactions)}, withheld bytes absent):")
    for r in export.redactions:
        console.print(f"    {r.digest}  exemption (claimed): {r.exemption_ref}")

    raise typer.Exit(0)
