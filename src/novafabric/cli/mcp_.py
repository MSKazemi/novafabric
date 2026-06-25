"""nova mcp — MCP supply-chain risk scanner (E-9)."""
from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(name="mcp", help="Scan MCP servers for supply-chain risks.")


class ScanThreshold(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


console = Console()
err_console = Console(stderr=True)


@app.command("scan")
def scan_cmd(
    manifest: Annotated[
        Path,
        typer.Argument(help="Path to MCP server manifest (JSON or YAML)."),
    ],
    threshold: Annotated[
        ScanThreshold,
        typer.Option("--threshold", help="Minimum severity to fail on."),
    ] = ScanThreshold.HIGH,
) -> None:
    """Scan an MCP server manifest for OWASP LLM supply-chain risks.

    Checks for tool poisoning, server impersonation, and other supply-chain
    attack vectors. Exits 0 if no findings at or above the threshold.
    Exits 1 otherwise.

    Scope: single manifest file.

    \b
    Examples:
      nova mcp scan path/to/mcp-manifest.json
      nova mcp scan path/to/mcp-manifest.yaml --threshold MEDIUM
    """
    from novafabric.mcp_scanner.models import RiskSeverity  # noqa: F401
    from novafabric.mcp_scanner.scanner import RiskScanner

    if not manifest.exists():
        err_console.print(f"[red]File not found:[/red] {manifest}")
        raise typer.Exit(2)

    scanner = RiskScanner()
    report = scanner.scan_file(manifest)

    _severity_rank = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0}
    threshold_rank = _severity_rank.get(threshold.upper(), 3)
    fail = any(
        _severity_rank.get(f.severity.value, 0) >= threshold_rank
        for tool in report.tools
        for f in tool.findings
    )

    table = Table(title=f"MCP Risk Scan: {report.server_name}", show_header=True)
    table.add_column("Tool")
    table.add_column("Category")
    table.add_column("Severity")
    table.add_column("Message")

    for tool in report.tools:
        for finding in tool.findings:
            style = {
                "CRITICAL": "red bold",
                "HIGH": "red",
                "MEDIUM": "yellow",
                "LOW": "blue",
                "INFO": "dim",
            }.get(finding.severity.value, "")
            table.add_row(
                finding.tool_name,
                finding.category.value,
                finding.severity.value,
                finding.message,
                style=style,
            )

    console.print(table)
    console.print(f"\nOverall risk level: [bold]{report.overall_risk_level}[/bold]")
    console.print(f"Total findings: {report.total_findings}")

    if fail:
        raise typer.Exit(1)


@app.command("risk-report")
def risk_report_cmd(
    manifest: Annotated[
        Path,
        typer.Argument(help="Path to MCP server manifest (JSON or YAML)."),
    ],
    format: Annotated[
        str,
        typer.Option("--format", help="Output format: rich|json."),
    ] = "rich",
) -> None:
    """Generate a structured OWASP LLM risk report for an MCP server manifest.

    Scope: single manifest file.

    \b
    Examples:
      nova mcp risk-report path/to/mcp-manifest.json
      nova mcp risk-report path/to/mcp-manifest.json --format json
    """
    from novafabric.mcp_scanner.scanner import RiskScanner

    if not manifest.exists():
        err_console.print(f"[red]File not found:[/red] {manifest}")
        raise typer.Exit(2)

    scanner = RiskScanner()
    report = scanner.scan_file(manifest)

    if format == "json":
        print(json.dumps(report.model_dump_report(), indent=2))
        return

    console.print(f"\n[bold]MCP Risk Report — {report.server_name}[/bold]")
    console.print(f"Overall risk level: [bold]{report.overall_risk_level}[/bold]")
    console.print(f"Total tools scanned: {len(report.tools)}")
    console.print(f"Total findings: {report.total_findings}")

    for tool in report.tools:
        if not tool.findings:
            continue
        console.print(f"\n  Tool: [cyan]{tool.tool_name}[/cyan] (score {tool.risk_score:.1f})")
        for f in tool.findings:
            console.print(f"    [{f.severity.value}] {f.category.value} — {f.message}")
            console.print(f"      Evidence: {f.evidence}")
