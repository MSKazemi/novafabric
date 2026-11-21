from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from novafabric._paths import default_capsule_dir

if TYPE_CHECKING:
    from novafabric.compliance.euaiact import EuAiActConfig

console = Console()

euaiact_app = typer.Typer(
    name="euaiact",
    help="EU AI Act Article 12 compliance: export structured logs for authority access requests.",
    no_args_is_help=True,
)


def _retention_line(cfg: EuAiActConfig) -> str:
    """One line separating the Act's log floor from the optional documentation horizon."""
    line = (
        f"Log retention floor: {cfg.log_retention_floor_months} months "
        "(Art. 19(1) / Art. 26(6))"
    )
    if cfg.documentation_retention_months is not None:
        line += (
            f"; documentation horizon applied: {cfg.documentation_retention_months} "
            "months (Art. 18(1), provider mode)"
        )
    return line


@euaiact_app.command("export")
def euaiact_export_cmd(
    from_date: Annotated[
        Optional[str],
        typer.Option(
            "--from",
            help="Start date ISO 8601 (inclusive, UTC). Default: no lower bound.",
        ),
    ] = None,
    to_date: Annotated[
        Optional[str],
        typer.Option(
            "--to",
            help="End date ISO 8601 (inclusive, UTC). Default: now.",
        ),
    ] = None,
    capsules_dir: Annotated[
        Optional[Path],
        typer.Option(
            "--capsules-dir",
            help=(
                "Directory containing capsules. "
                "Default: $NOVAFABRIC_CAPSULE_DIR or $NOVAFABRIC_HOME/capsules."
            ),
        ),
    ] = None,
    output: Annotated[
        Optional[str],
        typer.Option("--output", "-o", help="Output file path. Default: stdout (JSON)."),
    ] = None,
    pretty: Annotated[
        bool,
        typer.Option(
            "--pretty/--no-pretty",
            help="Pretty-print Rich table to terminal instead of JSON.",
        ),
    ] = False,
) -> None:
    """Export a structured activity log for an EU AI Act authority access request.

    Art. 21(2) of the EU AI Act requires providers, on a reasoned request, to give
    the competent authority access to the automatically generated logs under their
    control.

    Scope: single capsule.

    \b
    Examples:
      nova euaiact export path/to/my-capsule/
      nova euaiact export path/to/my-capsule/ --output euaiact-export.json
    """
    from novafabric.compliance.euaiact import EuAiActConfig, EuAiActExporter

    cfg = EuAiActConfig()
    if not cfg.high_risk:
        console.print(
            "[yellow]![/yellow] NOVA_EUAIACT_HIGH_RISK is not set. "
            "Art.12 compliance mode is inactive — export proceeds anyway."
        )

    from_dt: datetime | None = None
    to_dt: datetime | None = None

    if from_date:
        try:
            from_dt = datetime.fromisoformat(from_date.rstrip("Z")).replace(tzinfo=timezone.utc)
        except ValueError:
            console.print(f"[red]x[/red] Invalid --from date: {from_date!r}")
            raise typer.Exit(code=1)

    if to_date:
        try:
            to_dt = datetime.fromisoformat(to_date.rstrip("Z")).replace(tzinfo=timezone.utc)
        except ValueError:
            console.print(f"[red]x[/red] Invalid --to date: {to_date!r}")
            raise typer.Exit(code=1)

    resolved_dir = (capsules_dir or default_capsule_dir()).resolve()
    exporter = EuAiActExporter(resolved_dir)
    records = exporter.export(from_dt=from_dt, to_dt=to_dt)

    if not records:
        console.print("[yellow]![/yellow] No capsules found in the specified date range.")
        raise typer.Exit(code=0)

    if pretty:
        table = Table(title=f"Art.12 Log Export — {len(records)} capsule(s)", show_lines=False)
        table.add_column("run_id", style="cyan", no_wrap=True, max_width=26)
        table.add_column("created_at", style="dim")
        table.add_column("status")
        table.add_column("logging_event_types")
        for rec in records:
            table.add_row(
                str(rec.get("run_id", ""))[:26],
                str(rec.get("created_at", "")),
                str(rec.get("status", "")),
                ", ".join(rec.get("logging_event_type", [])),
            )
        console.print(table)
        console.print(f"[dim]{_retention_line(cfg)}[/dim]")
    else:
        payload = json.dumps(records, indent=2, default=str)
        if output:
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            Path(output).write_text(payload, encoding="utf-8")
            console.print(
                f"[green]✓[/green] Art.12 export written to {output} ({len(records)} records)"
            )
        else:
            typer.echo(payload)


@euaiact_app.command("status")
def euaiact_status_cmd() -> None:
    """Show EU AI Act compliance status for a capsule.

    Scope: single capsule.

    \b
    Examples:
      nova euaiact status path/to/my-capsule/
    """
    from novafabric.compliance.euaiact import EuAiActConfig

    cfg = EuAiActConfig()
    table = Table(title="EU AI Act Art.12 Configuration", show_header=False)
    table.add_column("Key", style="bold")
    table.add_column("Value")
    table.add_row(
        "NOVA_EUAIACT_HIGH_RISK",
        "[green]true[/green]" if cfg.high_risk else "[dim]false (inactive)[/dim]",
    )
    table.add_row(
        "NOVA_EUAIACT_PROVIDER",
        "[green]true[/green]" if cfg.provider_mode else "[dim]false (deployer mode)[/dim]",
    )
    table.add_row(
        "Log retention floor",
        f"{cfg.log_retention_floor_months} months (Art. 19(1) providers / Art. 26(6) deployers)",
    )
    table.add_row(
        "Documentation horizon",
        f"{cfg.documentation_retention_months} months "
        "(Art. 18(1) documentation, not logs; applied to capsules as policy)"
        if cfg.documentation_retention_months is not None
        else "[dim]not applied (set NOVA_EUAIACT_PROVIDER=true)[/dim]",
    )
    table.add_row("Applied retention", f"{cfg.retention_months} months")
    table.add_row(
        "Art.50 deadline",
        "2026-08-02",
    )
    console.print(table)
