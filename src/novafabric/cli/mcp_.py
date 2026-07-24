"""nova mcp — MCP supply-chain risk scanner (E-9)."""
from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Optional

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


card_app = typer.Typer(
    name="card",
    help="SEP-1649 MCP Server Card — the discovery document at /.well-known/mcp.json.",
    no_args_is_help=True,
)
app.add_typer(card_app, name="card")


@card_app.command("show")
def card_show(
    base_url: Annotated[
        Optional[str],
        typer.Option(
            "--base-url",
            help="Public base URL to advertise (default: derived from server config).",
        ),
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit the card as JSON.")
    ] = False,
) -> None:
    """Print the Server Card `nova serve` would publish (NF-039, SEP-1649).

    Generated from the live server configuration, never hand-written: a
    discovery document that has drifted from what the server actually does is
    worse than none, because a client trusts it precisely for being
    authoritative.

    \b
    Examples:
      nova mcp card show
      nova mcp card show --json --base-url https://nova.example.com
    """
    import json as _json

    from novafabric.mcp.servercard import build_server_card

    config = None
    try:
        from novafabric.server.config import load_config  # noqa: PLC0415

        config = load_config()
    except Exception:  # noqa: BLE001 — no [server] extra / no config is fine
        config = None

    card = build_server_card(config, base_url=base_url)
    payload = card.model_dump(mode="json", exclude_none=True)

    if json_out:
        typer.echo(_json.dumps(payload, indent=2))
        return
    typer.echo(f"name:            {card.name}")
    typer.echo(f"protocolVersion: {card.protocolVersion}")
    typer.echo(f"auth:            {card.auth.type}"
               + (f" (issuer={card.auth.issuer})" if card.auth.issuer else ""))
    typer.echo(f"capabilities:    {', '.join(sorted(card.capabilities))}")
    for endpoint in card.endpoints:
        typer.echo(f"endpoint:        {endpoint.transport} {endpoint.url}")


@card_app.command("validate")
def card_validate(
    target: Annotated[
        str,
        typer.Argument(help="Path to a Server Card JSON file, or an http(s) URL."),
    ],
) -> None:
    """Validate a Server Card against SEP-1649 (NF-039 R8).

    Strict about structure, permissive about unknown keys: SEP-1649 is an
    evolving format, so an unrecognised field is forward-compatibility rather
    than an error — but a missing required field means a client cannot rely on
    the document, which is the one thing it exists to be.

    \b
    Examples:
      nova mcp card validate card.json
      nova mcp card validate https://nova.example.com/.well-known/mcp.json
    """
    import json as _json
    from pathlib import Path as _Path

    from novafabric.mcp.servercard import (
        ServerCardValidationError,
        validate_server_card,
    )

    if target.startswith(("http://", "https://")):
        try:
            import urllib.request  # noqa: PLC0415

            with urllib.request.urlopen(target, timeout=10) as response:  # noqa: S310
                document = _json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            typer.echo(f"Error: could not fetch {target}: {exc}", err=True)
            raise typer.Exit(code=2) from exc
    else:
        path = _Path(target)
        if not path.is_file():
            typer.echo(f"Error: no such file: {target}", err=True)
            raise typer.Exit(code=2)
        try:
            document = _json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            typer.echo(f"Error: could not read {target}: {exc}", err=True)
            raise typer.Exit(code=2) from exc

    try:
        card = validate_server_card(document)
    except ServerCardValidationError as exc:
        typer.echo(f"✗ invalid Server Card: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"✓ valid SEP-1649 Server Card — {card.name} ({card.protocolVersion})")


@app.command("conformance")
def mcp_conformance(
    vectors_dir: Annotated[
        Path,
        typer.Argument(
            help="Directory of MCP conformance vector JSON files.",
            exists=True,
            file_okay=False,
        ),
    ],
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit the results as JSON.")
    ] = False,
) -> None:
    """Replay MCP conformance vectors and assert wire compatibility (NF-038 R9).

    Each vector pins a wire behaviour of MCP 2026-07-28 against the capture
    shape it must produce, so a spec drift fails loudly here rather than
    surfacing as silently-wrong evidence later.

    Exits 1 if any vector fails.

    \b
    Examples:
      nova mcp conformance tests/mcp/vectors/
      nova mcp conformance tests/mcp/vectors/ --json
    """
    import json as _json

    from novafabric.mcp.exchanges import TASKS_EXTENSION_KEY, ExchangeTracker

    results: list[dict[str, Any]] = []
    for path in sorted(vectors_dir.glob("*.json")):
        try:
            vector = _json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            results.append({"vector": path.name, "ok": False, "error": str(exc)})
            continue

        tracker = ExchangeTracker()
        records = []
        for leg in vector.get("messages", []):
            record = tracker.observe(
                leg["message"],
                direction=leg["direction"],
                protocol_version=vector.get("protocol_version"),
            )
            if record is not None:
                records.append(record)

        expect = vector.get("expect", {})
        failures: list[str] = []
        if "record_count" in expect and len(records) != expect["record_count"]:
            failures.append(
                f"record_count {len(records)} != {expect['record_count']}"
            )
        if "rounds" in expect and [r["round"] for r in records] != expect["rounds"]:
            failures.append("round structure drifted")
        if "directions" in expect and (
            [r["direction"] for r in records] != expect["directions"]
        ):
            failures.append("leg directions drifted")
        if expect.get("shared_exchange_id") and (
            len({r["mcp_exchange_id"] for r in records}) != 1
        ):
            failures.append("legs split across multiple exchange ids")
        if "distinct_exchange_ids" in expect and (
            len({r["mcp_exchange_id"] for r in records})
            != expect["distinct_exchange_ids"]
        ):
            failures.append("wrong number of distinct exchanges")
        if expect.get("tasks_extension_present") and not any(
            TASKS_EXTENSION_KEY in (r.get("extensions") or {}) for r in records
        ):
            failures.append("Tasks extension dropped")
        blob = _json.dumps(records)
        for forbidden in expect.get("no_raw_values", []):
            if forbidden in blob:
                failures.append(f"raw value leaked: {forbidden!r}")

        results.append(
            {
                "vector": path.name,
                "name": vector.get("name", path.stem),
                "ok": not failures,
                "failures": failures,
                # Printed on failure so the reader learns what broke in the
                # product, not merely which assertion tripped.
                "why": vector.get("why", ""),
            }
        )

    passed = sum(1 for r in results if r["ok"])
    failed = len(results) - passed

    if json_out:
        typer.echo(_json.dumps({"passed": passed, "failed": failed, "results": results}, indent=2))
    else:
        for result in results:
            mark = "✓" if result["ok"] else "✗"
            typer.echo(f"{mark} {result.get('name', result['vector'])}")
            for failure in result.get("failures", []) or []:
                typer.echo(f"    {failure}")
            if not result["ok"] and result.get("why"):
                typer.echo(f"    why this matters: {result['why']}")
        typer.echo(f"\n{passed} passed, {failed} failed")

    if not results:
        typer.echo("Error: no vectors found — a suite with no vectors proves nothing.", err=True)
        raise typer.Exit(code=2)
    if failed:
        raise typer.Exit(code=1)
