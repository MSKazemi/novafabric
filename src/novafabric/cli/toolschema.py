"""nova toolschema — tool-schema supply-chain evidence (ADR-0148 D2, experimental).

Read-only. ``nova toolschema impact`` re-validates the historical captured tool-call payloads in a
document against a **new** schema and reports exactly which runs would break under it (with per-run
failing paths). It reuses the shipped ADR-0128 validator — it does not reimplement validation.

Impact analysis is **evidence, not a gate**: the command exits ``0`` whether or not any run breaks
(``2`` only on bad input), so it can run in CI without blocking.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

app = typer.Typer(
    name="toolschema",
    help="Tool-schema replay-impact analysis over historical payloads (experimental, ADR-0148).",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)


@app.command("impact")
def impact(
    document: Annotated[
        Path,
        typer.Argument(help="JSON: {tool_id, tool_calls:[{run_id, arguments}]}."),
    ],
    new_schema: Annotated[
        Path,
        typer.Option("--new-schema", help="Path to the new JSON Schema to test past runs against."),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the schema_impact report as JSON."),
    ] = False,
) -> None:
    """Report which historical runs break under a new tool schema (reuses the ADR-0128 validator).

    \b
    Examples:
      nova toolschema impact calls.json --new-schema new.json
      nova toolschema impact calls.json --new-schema new.json --json
    """
    from novafabric.supplychain.toolschema.impact import compute_schema_impact

    if not document.exists():
        err_console.print(f"[red]Document not found:[/red] {document}")
        raise typer.Exit(2)
    try:
        doc = json.loads(document.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        err_console.print(f"[red]Could not read document:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("tool_calls"), list):
        err_console.print("[red]Document must be an object with a 'tool_calls' list.[/red]")
        raise typer.Exit(2)

    try:
        report = compute_schema_impact(
            tool_id=str(doc.get("tool_id", "")),
            new_schema_path=new_schema,
            tool_calls=doc["tool_calls"],
        )
    except (ValueError, TypeError) as exc:
        err_console.print(f"[red]Invalid impact input:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(report.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    n_broken = len(report.broken_run_ids)
    head = (
        f"[red]{n_broken} run(s) break[/red]" if n_broken else "[green]no runs break[/green]"
    )
    console.print(
        f"Tool-schema impact for {report.tool_id or '(unnamed tool)'} — {head} "
        f"of {report.checked} checked (evidence, not a gate)"
    )
    for b in report.broken_run_ids:
        console.print(f"  {b.run_id}: {', '.join(b.failing_paths)}")

    raise typer.Exit(0)
