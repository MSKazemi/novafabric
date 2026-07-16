"""nova export-accessibility-claim — declared accessibility claim (ADR-0169 D5 / NF-380).

Read-only. Loads the accessibility inputs and renders a declared accessibility-conformance claim
over a public disclosure: a ``declared_standard`` (``wcag_2_2_aa`` / ``en_301_549_v4_1_1``), an
``audit_digest`` (a record-only reference to a *declared* audit), and an ``export_format_check``
(did the exporter emit an accessible artifact shape).

The standard comes from the document's ``declared_standard`` or the ``--standard`` flag (the flag
wins). It records a **declared** claim — NovaFabric performs **no** accessibility audit and
guarantees no conformance.

Exit codes: 0 — rendered; 2 — the input is missing/malformed, or the standard is absent/invalid.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

console = Console()
err_console = Console(stderr=True)


def export_accessibility_claim_cmd(
    document: Annotated[
        Path,
        typer.Argument(help="JSON: {declared_standard?, audit_digest?, export_format_check?}."),
    ],
    standard: Annotated[
        str | None,
        typer.Option(
            "--standard",
            help="Declared standard (wcag_2_2_aa | en_301_549_v4_1_1); overrides the document.",
        ),
    ] = None,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the claim as JSON."),
    ] = False,
) -> None:
    """Render a declared accessibility-conformance claim — a declared claim, never a guarantee.

    \b
    Examples:
      nova export-accessibility-claim a11y.json
      nova export-accessibility-claim a11y.json --standard en_301_549_v4_1_1 --json
    """
    from novafabric.compliance.export.public._accessibility import build_accessibility_claim

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

    declared = standard or doc.get("declared_standard") or ""
    try:
        claim = build_accessibility_claim(
            declared_standard=str(declared),
            audit_digest=doc.get("audit_digest"),
            export_format_check=bool(doc.get("export_format_check", False)),
        )
    except (ValueError, TypeError) as exc:
        err_console.print(f"[red]Invalid document:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(claim.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    console.print("Accessibility claim (declared — NovaFabric performs no audit)")
    console.print(f"  declared_standard:   {claim.declared_standard}")
    console.print(f"  audit_digest:        {claim.audit_digest}")
    console.print(f"  export_format_check: {claim.export_format_check}")

    raise typer.Exit(0)
