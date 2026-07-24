from __future__ import annotations

from enum import Enum
from typing import Annotated, Optional

import typer
from rich.console import Console

from novafabric.report.generator import generate_report, generate_report_pdf

console = Console()


class ReportFormat(str, Enum):
    markdown = "markdown"
    json = "json"
    html = "html"
    pdf = "pdf"


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

      # Self-contained HTML with an assets-by-type chart (ADR-0201)
      nova report --format html --output inventory.html

      # PDF (requires the optional WeasyPrint extra "novafabric compliance")
      nova report --format pdf --output inventory.pdf
    """
    if format_ is ReportFormat.pdf:
        if not output:
            console.print("[red]--output is required for --format pdf[/red]")
            raise typer.Exit(code=1)
        try:
            pdf = generate_report_pdf()
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        with open(output, "wb") as fb:
            fb.write(pdf)
        console.print(f"[green]Report written to {output}[/green]")
        return

    content = generate_report(format_.value)
    if output:
        with open(output, "w") as f:
            f.write(content)
        console.print(f"[green]Report written to {output}[/green]")
    else:
        console.print(content)
