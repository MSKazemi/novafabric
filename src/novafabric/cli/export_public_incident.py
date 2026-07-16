"""nova export-public-incident — DRAFT public-interest incident summary (ADR-0169 D1 / NF-378).

Read-only. Loads a sealed incident's public-facing fields and renders a DRAFT public-audience
incident summary: ``incident_ref``, ``public_summary``, ``affected_scope`` (aggregate — no
per-subject data), and ``remediation_ref``. It is always a **DRAFT, never transmitted** (NovaFabric
never publishes or notifies), and it **adjudicates nothing** — no ``compliance_guaranteed`` claim.

The summary and scope must be **aggregate**: if either carries a per-subject raw identifier (an SSN,
an email, …) the export **refuses** rather than turning a public summary into a per-subject
disclosure.

Exit codes: 0 — rendered; 2 — input missing/malformed, or a per-subject identifier is present.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

console = Console()
err_console = Console(stderr=True)


def export_public_incident_cmd(
    document: Annotated[
        Path,
        typer.Argument(
            help="JSON: {incident_ref, public_summary?, affected_scope?, remediation_ref?}."
        ),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the DRAFT disclosure as JSON."),
    ] = False,
) -> None:
    """Render a DRAFT public-interest incident summary from a sealed incident.

    \b
    Examples:
      nova export-public-incident incident.json
      nova export-public-incident incident.json --json
    """
    from novafabric.compliance.export.public._public_incident import (
        build_public_incident_disclosure,
    )

    if not document.exists():
        err_console.print(f"[red]Document not found:[/red] {document}")
        raise typer.Exit(2)
    try:
        doc = json.loads(document.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        err_console.print(f"[red]Could not read document:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict) or not doc.get("incident_ref"):
        err_console.print("[red]Document must be a JSON object with 'incident_ref'.[/red]")
        raise typer.Exit(2)

    try:
        disclosure = build_public_incident_disclosure(
            incident_ref=str(doc["incident_ref"]),
            public_summary=doc.get("public_summary"),
            affected_scope=doc.get("affected_scope"),
            remediation_ref=doc.get("remediation_ref"),
        )
    except (ValueError, TypeError) as exc:
        # Includes the per-subject-identifier refusal (exit 2).
        err_console.print(f"[red]Refused:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(disclosure.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    draft = "DRAFT — never transmitted" if disclosure.draft else "?"
    console.print(f"Public-interest incident disclosure ({draft})")
    console.print(f"  incident_ref:    {disclosure.incident_ref}")
    console.print(f"  public_summary:  {disclosure.public_summary}")
    console.print(f"  affected_scope:  {disclosure.affected_scope}")
    console.print(f"  remediation_ref: {disclosure.remediation_ref}")

    raise typer.Exit(0)
