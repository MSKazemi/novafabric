from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from novafabric.capsule.env_contract import SchedulerEnvDiagnosis, diagnose_scheduler_env
from novafabric.storage import StorageInfo, get_backend

console = Console()


def doctor_cmd(
    check_storage: Annotated[
        bool,
        typer.Option(
            "--check-storage",
            help=(
                "Report backend name, schema version, migration status, "
                "and row counts per table (ADR-0016)."
            ),
        ),
    ] = False,
    check_scheduler: Annotated[
        bool,
        typer.Option(
            "--check-scheduler",
            help=(
                "Detect a scheduler-vs-NOVAFABRIC_*-env-var mismatch, e.g. a "
                "Slurm site's --export=NONE policy silently dropping capture "
                "identity (OQ-06, PAR-ADR-003)."
            ),
        ),
    ] = False,
    db_path: Annotated[
        Optional[Path],
        typer.Option(
            "--db-path",
            help="Path to the SQLite DB (overrides NOVAFABRIC_DB_PATH).",
            show_default=False,
        ),
    ] = None,
    backend: Annotated[
        Optional[str],
        typer.Option(
            "--backend",
            help="Backend to inspect: sqlite (default) or postgres.",
            show_default=False,
        ),
    ] = None,
    postgres_dsn: Annotated[
        Optional[str],
        typer.Option(
            "--postgres-dsn",
            help="Postgres DSN (overrides NOVAFABRIC_POSTGRES_DSN).",
            show_default=False,
            envvar="NOVAFABRIC_POSTGRES_DSN",
        ),
    ] = None,
) -> None:
    """Run diagnostic checks on the installation.

    Reports directory structure, key presence, database health, schema
    version, and storage backend status.

    Scope: global (checks the whole installation).

    \b
    Examples:
      # Basic health check
      nova doctor

      # Include storage backend checks
      nova doctor --check-storage

      # Detect a scheduler/env-var contract mismatch (OQ-06)
      nova doctor --check-scheduler

      # Check a specific database path
      nova doctor --db-path ~/custom/novafabric.db
    """
    if not check_storage and not check_scheduler:
        console.print(
            "[yellow]Hint:[/yellow] pass [bold]--check-storage[/bold] to inspect "
            "the storage backend, or [bold]--check-scheduler[/bold] to detect a "
            "scheduler/env-var contract mismatch."
        )
        return

    failed = False

    if check_storage:
        try:
            storage = get_backend(
                backend=backend,
                db_path=db_path,
                postgres_dsn=postgres_dsn,
            )
            info = storage.info()
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(1) from exc

        _print_storage_report(info)

    if check_scheduler:
        diagnosis = diagnose_scheduler_env()
        _print_scheduler_report(diagnosis)
        if not diagnosis.ok:
            failed = True

    if failed:
        raise typer.Exit(1)


def _print_storage_report(info: StorageInfo) -> None:
    console.print()
    console.print("[bold]Storage backend[/bold]")

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="dim")
    summary.add_column()

    summary.add_row("Backend", f"[cyan]{info.backend}[/cyan]")

    if info.db_path:
        summary.add_row("DB path", info.db_path)

    if info.schema_version is not None:
        summary.add_row("Schema version", str(info.schema_version))
    elif info.backend == "sqlite":
        summary.add_row("Schema version", "[dim]not initialised[/dim]")

    if info.migration_pending is True:
        # ADR-0211 D5: name the registry track explicitly — a bare
        # `nova db upgrade` migrates the MetadataStore tier, not this DB.
        summary.add_row(
            "Migrations",
            "[yellow]pending[/yellow] — run [bold]nova db upgrade "
            f"--track registry --backend {info.backend}[/bold]",
        )
    elif info.migration_pending is False:
        summary.add_row("Migrations", "[green]up to date[/green]")
    else:
        summary.add_row("Migrations", "[dim]not checked[/dim]")

    console.print(summary)

    if info.row_counts:
        console.print()
        console.print("[bold]Row counts[/bold]")
        tbl = Table(show_header=True, header_style="bold")
        tbl.add_column("Table")
        tbl.add_column("Rows", justify="right")
        for table, count in sorted(info.row_counts.items()):
            tbl.add_row(table, str(count))
        console.print(tbl)
    elif info.db_path:
        console.print()
        console.print("[dim]No tables found — database may not be initialised.[/dim]")


def _print_scheduler_report(diagnosis: SchedulerEnvDiagnosis) -> None:
    console.print()
    console.print("[bold]Scheduler / env-var contract[/bold]")

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="dim")
    summary.add_column()

    summary.add_row(
        "Scheduler detected",
        diagnosis.scheduler_detected or "[dim]none[/dim]",
    )
    summary.add_row(
        "NOVAFABRIC_* contract vars",
        "[green]present[/green]" if diagnosis.contract_vars_present else "[yellow]absent[/yellow]",
    )
    if diagnosis.slurm_export_env is not None:
        summary.add_row("SLURM_EXPORT_ENV", diagnosis.slurm_export_env)

    console.print(summary)

    if diagnosis.ok:
        console.print()
        console.print("[green]OK[/green] — no scheduler/env-var contract mismatch detected.")
        return

    console.print()
    console.print("[red]Mismatch detected[/red]")
    for issue in diagnosis.issues:
        console.print(f"  - {issue}")
    if diagnosis.remediation:
        console.print()
        console.print("[bold]Remediation[/bold]")
        for hint in diagnosis.remediation:
            console.print(f"  - {hint}")

    console.print()
