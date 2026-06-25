from __future__ import annotations

from enum import Enum
from typing import Annotated, Optional

import typer
from rich.console import Console

from novafabric.report.generator import generate_report

console = Console()


class ReportFormat(str, Enum):
    markdown = "markdown"
    json = "json"


def report_cmd(
    format_: Annotated[
        ReportFormat,
        typer.Option("--format", "-f", help="Output format."),
    ] = ReportFormat.markdown,
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Write to file"),
) -> None:
    """Generate an asset inventory report.

    Scope: registry-wide.

    \b
    Examples:
      # Print a markdown report to stdout
      nova report

      # Export as JSON
      nova report --format json

      # Save to a file
      nova report --format json --output inventory.json
    """
    content = generate_report(format_.value)
    if output:
        with open(output, "w") as f:
            f.write(content)
        console.print(f"[green]Report written to {output}[/green]")
    else:
        console.print(content)
