"""nova export-control-attestation — governance-control attestation (ADR-0170 D5 / NF-387).

Read-only. Loads a declared control catalog and the governance evidence present for a capsule and
renders an attestation pack, mapping each premium-relevant governance control to the **shipped**
NovaFabric evidence (sealing, HITL/oversight, eval-gated promotion, redaction proofs), marked
``evidenced`` / ``not_evidenced`` / ``declared``.

It **presents** governance evidence for an insurer to reason over — it does **not** certify a
control is adequate (no certified/pass/verdict field). References and digests only, never PII.

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
    "evidenced": "[green]●[/green]",
    "not_evidenced": "[red]○[/red]",
    "declared": "[yellow]◐[/yellow]",
}


def export_control_attestation_cmd(
    document: Annotated[
        Path,
        typer.Argument(
            help="JSON: {capsule_root, catalog:[{control_id, evidence_kind?}], "
            "present_evidence{}, declared[]}."
        ),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the attestation pack as JSON."),
    ] = False,
) -> None:
    """Render a governance-control attestation pack — presents evidence, never certifies.

    \b
    Examples:
      nova export-control-attestation ctrl.json
      nova export-control-attestation ctrl.json --json
    """
    from novafabric.compliance.export.control_attestation import build_control_attestation

    if not document.exists():
        err_console.print(f"[red]Document not found:[/red] {document}")
        raise typer.Exit(2)
    try:
        doc = json.loads(document.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        err_console.print(f"[red]Could not read document:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict) or not doc.get("capsule_root"):
        err_console.print("[red]Document must be a JSON object with 'capsule_root'.[/red]")
        raise typer.Exit(2)

    try:
        pack = build_control_attestation(
            capsule_root=str(doc["capsule_root"]),
            catalog=doc.get("catalog", []),
            present_evidence=doc.get("present_evidence", {}),
            declared=doc.get("declared", []),
            catalog_ref=doc.get("catalog_ref"),
        )
    except (ValueError, TypeError, KeyError) as exc:
        err_console.print(f"[red]Invalid document:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(pack.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    s = pack.summary
    console.print(
        "Control attestation (presents governance evidence — does not certify)  "
        f"evidenced={s['evidenced']} not_evidenced={s['not_evidenced']} declared={s['declared']}"
    )
    for e in pack.entries:
        mark = _MARK.get(e.status.value, "?")
        detail = f"  {e.evidence_ref}" if e.evidence_ref else ""
        kind = f" ({e.evidence_kind})" if e.evidence_kind else ""
        console.print(f"  {mark} {e.control_id}{kind}: {e.status.value}{detail}")

    raise typer.Exit(0)
