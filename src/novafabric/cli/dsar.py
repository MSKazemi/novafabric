"""nova dsar — read-only subject-rights (DSAR) assembly over sealed evidence (ADR-0161).

This slice ships ``dsar assemble`` (D1): a deterministic cross-capsule assembly of every capsule
that processed a subject, keyed on the **HMAC pseudonym** — the raw subject id never enters the
artifact — and ``dsar sla`` (D7): the turnaround of a request against a deadline (default GDPR
Art. 12(3), one month). Future subcommands (``purpose-audit``, ``retention-audit`` — D3/D4) attach
here.

The commands reconstruct/compute only over already-sealed evidence and recorded timestamps supplied
as a JSON document; missing capsules are shown as ``gaps``, never raised (fail-open).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

dsar_app = typer.Typer(help="Read-only subject-rights (DSAR) assembly over sealed evidence.")
console = Console()
err_console = Console(stderr=True)


@dsar_app.command("assemble")
def assemble_cmd(
    document: Annotated[
        Path,
        typer.Argument(help="JSON: {subject_hmac, records:[{capsule_id, categories}], gaps?}."),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the DSAR package as JSON."),
    ] = False,
) -> None:
    """Assemble a subject's cross-capsule DSAR package (deterministic, keyed on HMAC pseudonym).

    \b
    Examples:
      nova dsar assemble subject-records.json
      nova dsar assemble subject-records.json --json
    """
    from novafabric.compliance.governance.dsar import assemble_dsar

    if not document.exists():
        err_console.print(f"[red]DSAR document not found:[/red] {document}")
        raise typer.Exit(2)
    try:
        doc = json.loads(document.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        err_console.print(f"[red]Could not read DSAR document:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict) or "subject_hmac" not in doc:
        err_console.print("[red]Document must be a JSON object with 'subject_hmac'.[/red]")
        raise typer.Exit(2)

    try:
        pkg = assemble_dsar(
            str(doc["subject_hmac"]), doc.get("records", []), gaps=doc.get("gaps", [])
        )
    except (KeyError, ValueError, TypeError) as exc:
        err_console.print(f"[red]Invalid DSAR record:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(pkg.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    console.print(
        f"DSAR package: subject {pkg.subject_hmac}  "
        f"({len(pkg.capsules)} capsule(s), {len(pkg.gaps)} gap(s))"
    )
    for cap in pkg.capsules:
        cats = ", ".join(cap.categories) if cap.categories else "-"
        purpose = f"  purpose={cap.purpose}" if cap.purpose else ""
        # categories rendered without "[...]", which rich would parse as markup.
        console.print(f"  {cap.capsule_id}  categories: {cats}{purpose}")
    for gap in pkg.gaps:
        console.print(f"  [yellow]gap:[/yellow] {gap} (capsule not reconstructable)")

    raise typer.Exit(0)


@dsar_app.command("sla")
def sla_cmd(
    document: Annotated[
        Path,
        typer.Argument(
            help="JSON: {request_open, fulfilled_at, deadline?, subject_hmac?} (ISO 8601 UTC)."
        ),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the SLA record as JSON."),
    ] = False,
) -> None:
    """Compute a DSAR's turnaround against a deadline (default GDPR Art. 12(3), one month).

    ``met_deadline`` is a factual ``fulfilled_at <= deadline`` comparison over recorded
    timestamps — evidence a request was met on time, not a compliance verdict — so the command exits
    ``0`` whether or not the deadline was met (``2`` only on bad input).

    \b
    Examples:
      nova dsar sla request.json
      nova dsar sla request.json --json
    """
    from novafabric.compliance.governance.dsar_sla import compute_dsar_sla

    if not document.exists():
        err_console.print(f"[red]SLA document not found:[/red] {document}")
        raise typer.Exit(2)
    try:
        doc = json.loads(document.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        err_console.print(f"[red]Could not read SLA document:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict) or "request_open" not in doc or "fulfilled_at" not in doc:
        err_console.print(
            "[red]Document needs 'request_open' and 'fulfilled_at' (ISO 8601 UTC).[/red]"
        )
        raise typer.Exit(2)

    try:
        deadline_raw = doc.get("deadline")
        record = compute_dsar_sla(
            request_open=datetime.fromisoformat(str(doc["request_open"])),
            fulfilled_at=datetime.fromisoformat(str(doc["fulfilled_at"])),
            deadline=(datetime.fromisoformat(str(deadline_raw)) if deadline_raw else None),
            subject_hmac=doc.get("subject_hmac"),
        )
    except (ValueError, TypeError) as exc:
        err_console.print(f"[red]Invalid SLA input:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(record.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    days = record.turnaround_seconds / 86400
    flag = "[green]met deadline[/green]" if record.met_deadline else "[red]past deadline[/red]"
    console.print(
        f"DSAR SLA — turnaround {days:.1f}d, deadline {record.deadline.isoformat()}: {flag}"
    )
    console.print(f"  basis: {record.basis}")

    raise typer.Exit(0)
