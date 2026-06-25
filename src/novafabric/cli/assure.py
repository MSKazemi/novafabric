"""nova assure — OWASP Top 10 for LLM evidence report (E-10)."""
from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

console = Console()
err_console = Console(stderr=True)


class AssureOutputFormat(str, Enum):
    rich = "rich"
    json = "json"


def assure_cmd(
    capsule_path: Annotated[
        Path,
        typer.Argument(help="Path to capsule directory (or run ID resolved via NOVAFABRIC_HOME)."),
    ],
    format: Annotated[
        AssureOutputFormat,
        typer.Option("--format", help="Output format."),
    ] = AssureOutputFormat.rich,
) -> None:
    """Run OWASP Top 10 for LLMs evidence checks against a capsule.

    Checks for evidence of prompt injection protection, output sanitisation,
    model theft prevention, and other OWASP LLM Top 10 (2025) controls.
    Exits 0 if all checks pass or warn; exits 1 if any check fails.

    Scope: single capsule.

    \b
    Examples:
      nova assure path/to/my-capsule/
      nova assure --format json path/to/my-capsule/
    """
    from novafabric.assure.checker import AssuranceChecker
    from novafabric.assure.models import CheckStatus

    if not capsule_path.exists():
        err_console.print(f"[red]Capsule directory not found:[/red] {capsule_path}")
        raise typer.Exit(2)

    checker = AssuranceChecker()
    report = checker.check_all(capsule_path)

    if format == "json":
        data = report.model_dump(mode="json")
        print(json.dumps(data, indent=2))
        raise typer.Exit(0 if report.overall_status != CheckStatus.FAIL else 1)

    # Rich output
    status_colors = {
        "PASS": "[green]PASS[/green]",
        "FAIL": "[red bold]FAIL[/red bold]",
        "WARN": "[yellow]WARN[/yellow]",
        "SKIP": "[dim]SKIP[/dim]",
    }
    table = Table(
        title=f"OWASP LLM Assurance Report — {report.run_id}",
        show_header=True,
    )
    table.add_column("Check")
    table.add_column("Category")
    table.add_column("Status", width=6)
    table.add_column("Message")

    for r in report.results:
        table.add_row(
            r.check_id,
            r.category,
            status_colors.get(r.status.value, r.status.value),
            r.message,
        )

    console.print(table)
    summary = (
        f"({report.pass_count} pass / {report.warn_count} warn"
        f" / {report.fail_count} fail / {report.skip_count} skip)"
    )
    console.print(
        f"\nOverall: [bold]{report.overall_status.value}[/bold]  {summary}"
    )

    if report.overall_status == CheckStatus.FAIL:
        raise typer.Exit(1)
