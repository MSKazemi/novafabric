"""nova export-model-risk — SR 26-2 / SR 11-7 model-risk evidence pack (ADR-0159 D2 / NF-271).

Read-only. Loads a JSON of per-pillar evidence refs and renders the model-risk evidence *file*:
each of the four pillars (development, independent-validation, ongoing-monitoring, model-inventory)
marked complete / partial / missing, with source refs or a machine-readable reason. It **assembles,
never assesses** — there is no rating. This is the CLI half of NF-271; the collector that gathers
the refs from sealed evidence is a documented follow-on.

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

_MARK = {"complete": "[green]✓[/green]", "partial": "[yellow]◐[/yellow]", "missing": "[red]✗[/red]"}


def export_model_risk_cmd(
    evidence: Annotated[
        Path,
        typer.Argument(help="JSON: {model_id, development[], independent_validation[], "
                            "ongoing_monitoring[], model_inventory[], partial[]}."),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the model-risk file as JSON."),
    ] = False,
) -> None:
    """Assemble a SR 26-2 / SR 11-7 model-risk evidence file from per-pillar refs.

    \b
    Examples:
      nova export-model-risk evidence.json
      nova export-model-risk evidence.json --json
    """
    from novafabric.compliance.export.finance.model_risk import build_model_risk_file

    if not evidence.exists():
        err_console.print(f"[red]Evidence document not found:[/red] {evidence}")
        raise typer.Exit(2)
    try:
        doc = json.loads(evidence.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        err_console.print(f"[red]Could not read evidence document:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict) or "model_id" not in doc:
        err_console.print("[red]Document must be a JSON object with 'model_id'.[/red]")
        raise typer.Exit(2)

    try:
        mrf = build_model_risk_file(
            model_id=str(doc["model_id"]),
            development=doc.get("development", []),
            independent_validation=doc.get("independent_validation", []),
            ongoing_monitoring=doc.get("ongoing_monitoring", []),
            model_inventory=doc.get("model_inventory", []),
            partial=doc.get("partial", []),
        )
    except (ValueError, TypeError) as exc:
        err_console.print(f"[red]Invalid evidence document:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(mrf.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    s = mrf.summary
    # regime in parens, not "[...]", which rich would parse as markup.
    console.print(
        f"Model-risk file ({mrf.regime})  model: {mrf.model_id}   "
        f"complete={s['complete']} partial={s['partial']} missing={s['missing']}"
    )
    for p in mrf.pillars:
        mark = _MARK.get(p.status, "?")
        refs = f"  {', '.join(p.source_refs)}" if p.source_refs else ""
        reason = f"  ({p.reason})" if p.reason else ""
        console.print(f"  {mark} {p.pillar:<24} {p.status}{refs}{reason}")

    raise typer.Exit(0)
