"""nova export-whistleblower — source-protecting attestation over a sealed bundle (ADR-0169/NF-375).

Read-only. Loads a bundle's ``content_digest`` and ``authenticity_attestation`` (a reference to the
bundle's existing Evidence-Bundle Ed25519 signature — this slice never signs) plus an optional
``anonymity_set_ref``, and renders a tamper-evident, source-protecting statement: authenticity is
provable **without revealing the source**.

Source protection is a **hard invariant** (I-5): if the input carries any field matching a
source-identity / contact / routing shape (a ``submitter_email``, an ``ip_address``, …) the export
**refuses** and exits non-zero — it never silently carries such a field.

Exit codes: 0 — rendered; 2 — the input is missing/malformed, or it carries a source-identifying
field.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

console = Console()
err_console = Console(stderr=True)


def export_whistleblower_cmd(
    document: Annotated[
        Path,
        typer.Argument(
            help="JSON: {content_digest, authenticity_attestation, anonymity_set_ref?}."
        ),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the attestation as JSON."),
    ] = False,
) -> None:
    """Render a source-protecting whistleblower attestation over a sealed bundle.

    \b
    Examples:
      nova export-whistleblower wb.json
      nova export-whistleblower wb.json --json
    """
    from novafabric.compliance.export.public._whistleblower import (
        build_whistleblower_attestation,
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
        attestation = build_whistleblower_attestation(doc)
    except (ValueError, TypeError) as exc:
        # Includes the source-protection refusal — surface it plainly (exit 2).
        err_console.print(f"[red]Refused:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(attestation.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    console.print("Whistleblower attestation (tamper-evident, source-protecting)")
    console.print(f"  content_digest:           {attestation.content_digest}")
    console.print(f"  authenticity_attestation: {attestation.authenticity_attestation}")
    console.print(f"  anonymity_set_ref:        {attestation.anonymity_set_ref}")

    raise typer.Exit(0)
