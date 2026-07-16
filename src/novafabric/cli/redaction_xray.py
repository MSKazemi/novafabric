"""nova redaction-xray — project a capsule's redaction / secret-scan state.

ADR-0174 (data slice), read-only. Loads a JSON document describing a capsule's field-protection
state and prints a per-field state overlay + a coverage meter + per-state counts. This is the
CLI/JSON half of feature F-06; the `web/` heat-overlay tree is future design.

Input is a JSON object with EITHER:
  {"fields":   [{"path": ..., "state": "clear|redacted|secret_scrubbed|never_captured|unknown"}]}
  {"findings": [ raw MaskingPipeline finding records ]}   # adapted via field_states_from_findings

**No field value is ever printed** — the command surfaces field paths and states only
(ADR-0174 §1). Any value handed in alongside a record is dropped by the projection.

Exit codes: 0 — report rendered; 2 — input missing or malformed.
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
    "clear": "[dim]·[/dim]",
    "redacted": "[yellow]▨[/yellow]",
    "secret_scrubbed": "[red]■[/red]",
    "never_captured": "[dim]░[/dim]",
    "unknown": "[magenta]?[/magenta]",
}


def redaction_xray_cmd(
    document: Annotated[
        Path,
        typer.Argument(help="JSON with 'fields' (path+state) or raw 'findings'."),
    ],
    capsule_id: Annotated[
        str | None,
        typer.Option("--capsule-id", help="Capsule id to label the report with."),
    ] = None,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the X-Ray report as JSON."),
    ] = False,
) -> None:
    """Render a Redaction / Secret-scan X-Ray from a capsule's protection metadata.

    \b
    Examples:
      nova redaction-xray xray.json
      nova redaction-xray xray.json --json --capsule-id run-42
    """
    from novafabric.masking.xray import build_field_xray, field_states_from_findings

    if not document.exists():
        err_console.print(f"[red]X-Ray document not found:[/red] {document}")
        raise typer.Exit(2)
    try:
        doc = json.loads(document.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        err_console.print(f"[red]Could not read document:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict) or not ("fields" in doc or "findings" in doc):
        err_console.print("[red]Document must be a JSON object with 'fields' or 'findings'.[/red]")
        raise typer.Exit(2)

    try:
        if "findings" in doc:
            records = field_states_from_findings(doc["findings"])
        else:
            records = doc["fields"]
        report = build_field_xray(records, capsule_id=capsule_id)
    except (KeyError, ValueError, TypeError) as exc:
        err_console.print(f"[red]Invalid field/finding record:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        # model_dump carries path+state only — values can never be present here.
        print(json.dumps(report.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    cov = "n/a" if report.coverage is None else f"{report.coverage * 100:.0f}%"
    header = f"Redaction X-Ray: coverage {cov} of sensitive surface"
    if report.capsule_id:
        header += f"   capsule: {report.capsule_id}"
    console.print(header)
    console.print(
        "  counts: " + "  ".join(f"{k}={v}" for k, v in report.counts.items())
    )
    for field in report.fields:
        console.print(f"  {_MARK.get(field.state.value, '?')} {field.path}  ({field.state.value})")

    raise typer.Exit(0)
