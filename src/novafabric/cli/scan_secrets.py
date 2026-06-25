from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

console = Console()

_SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]


class ScanSeverity(str, Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    info = "info"


def _severity_at_or_above(threshold: str, severity: str) -> bool:
    return _SEVERITY_ORDER.index(severity) <= _SEVERITY_ORDER.index(threshold)


def scan_secrets_cmd(
    capsule_dir: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Path to a Run Capsule directory.",
    ),
    fail_on: ScanSeverity | None = typer.Option(
        None,
        "--fail-on",
        help="Exit 1 if any finding at or above this severity is found.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the full redaction proof as JSON to stdout. No human formatting.",
    ),
) -> None:
    """Report secrets and PII findings in a capsule's redaction log (read-only).

    Reads redaction-proof.json and displays all flagged findings. Use
    --fail-on to block CI pipelines when findings exceed a threshold.

    Scope: single capsule.

    \b
    Examples:
      nova scan-secrets path/to/my-capsule/
      nova scan-secrets path/to/my-capsule/ --fail-on high
    """
    proof_path = capsule_dir / "redaction-proof.json"
    if not proof_path.exists():
        console.print(
            f"[red]✗[/red] missing redaction-proof.json in {capsule_dir}\n"
            "  hint: run `nova redact <capsule>` to scan and redact."
        )
        raise typer.Exit(code=1)

    proof = json.loads(proof_path.read_text())

    if json_output:
        typer.echo(json.dumps(proof, indent=2))
        # When --json is set, --fail-on still gates exit code.
    else:
        _render_human(proof)

    if fail_on is not None:
        threshold = fail_on.lower()
        if threshold not in _SEVERITY_ORDER:
            console.print(
                f"[red]✗[/red] invalid --fail-on value: {fail_on!r}; "
                f"expected one of {_SEVERITY_ORDER}"
            )
            raise typer.Exit(code=1)
        triggers = [
            f for f in proof["findings"]
            if _severity_at_or_above(threshold, f["severity"])
        ]
        if triggers:
            if not json_output:
                console.print(
                    f"[red]✗[/red] {len(triggers)} finding(s) at or above "
                    f"severity={threshold}"
                )
            raise typer.Exit(code=2)


def _render_human(proof: dict[str, Any]) -> None:
    total = proof["findings_count"]["total"]
    by_sev = proof["findings_count"]["by_severity"]
    run_id = proof["capsule_run_id"]

    if total == 0:
        console.print(f"[green]✓[/green] {run_id}: no findings")
        return

    console.print(
        f"[yellow]![/yellow] {run_id}: {total} findings "
        f"(critical={by_sev['critical']}, high={by_sev['high']}, "
        f"medium={by_sev['medium']}, low={by_sev['low']})"
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("rule_id")
    table.add_column("severity")
    table.add_column("target")
    table.add_column("strategy")
    for finding in proof["findings"]:
        table.add_row(
            finding["rule_id"],
            finding["severity"],
            finding["target_ref"],
            finding["redaction_strategy"],
        )
    console.print(table)
