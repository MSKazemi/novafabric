from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from novafabric.capsule.env_contract import SchedulerEnvDiagnosis, diagnose_scheduler_env
from novafabric.cli._extras import (
    declared_extras,
    extra_requirements,
    missing_requirements,
    rich_install_command,
)
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
    check_tokens: Annotated[
        bool,
        typer.Option(
            "--check-tokens",
            help=(
                "Audit serve tokens whose secret is still stored in cleartext "
                "(pre-ADR-0252 records). Exits non-zero if any are found."
            ),
        ),
    ] = False,
    check_extras: Annotated[
        bool,
        typer.Option(
            "--check-extras",
            help=(
                "Report which optional extras are fully installed and name the "
                "exact command to install any that are not."
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

      # Which optional extras are installed, and how to install the rest
      nova doctor --check-extras

      # Include storage backend checks
      nova doctor --check-storage

      # Detect a scheduler/env-var contract mismatch (OQ-06)
      nova doctor --check-scheduler

      # Check a specific database path
      nova doctor --db-path ~/custom/novafabric.db
    """
    if not check_storage and not check_scheduler and not check_extras and not check_tokens:
        console.print(
            "[yellow]Hint:[/yellow] pass [bold]--check-extras[/bold] to see which "
            "optional extras are installed, [bold]--check-storage[/bold] to inspect "
            "the storage backend, [bold]--check-scheduler[/bold] to detect a "
            "scheduler/env-var contract mismatch, or [bold]--check-tokens[/bold] "
            "to audit serve tokens still stored in cleartext."
        )
        return

    failed = False

    if check_extras:
        _print_extras_report()

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

    # Legacy cleartext serve tokens (ADR-0252). Always reported, because an
    # operator who does not already know these exist is exactly the operator who
    # will not think to pass a flag asking about them. Only an explicit
    # --check-tokens makes it affect the exit code, so no existing invocation
    # changes its result.
    _print_legacy_token_report(explicit=check_tokens)
    if check_tokens and _legacy_token_count() > 0:
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


def _print_extras_report() -> None:
    """Report each optional extra as complete or incomplete, and how to fix it.

    Diagnostic only — it never changes the exit code. A missing extra is a
    deliberate choice for most installs (there are 32 of them, and nobody wants all
    of them), so treating absence as failure would make `nova doctor` red for
    almost everyone.
    """
    extras = declared_extras()

    console.print()
    console.print("[bold]Optional extras[/bold]")

    if not extras:
        console.print(
            "  [yellow]No extras declared in the installed metadata.[/yellow] This is "
            "expected when running from a source checkout rather than an installed "
            "distribution."
        )
        return

    table = Table.grid(padding=(0, 2))
    table.add_column(width=2)
    table.add_column(style="cyan", no_wrap=True)
    table.add_column()

    incomplete: list[str] = []
    for extra in extras:
        # `all` just re-exports the others; reporting it doubles every line.
        if extra == "all":
            continue
        required = extra_requirements(extra)
        if not required:
            continue
        missing = missing_requirements(extra)
        if missing:
            incomplete.append(extra)
            table.add_row("[red]✗[/red]", extra, f"[dim]missing:[/dim] {', '.join(missing)}")
        else:
            table.add_row("[green]✓[/green]", extra, f"[dim]{', '.join(required)}[/dim]")

    console.print(table)
    console.print()

    if not incomplete:
        console.print("  [green]All declared extras are fully installed.[/green]")
        return

    console.print(
        f"  {len(incomplete)} of {len(extras) - 1} extras incomplete. Features that "
        f"depend on them will fail at import."
    )
    console.print("  Install one with:")
    for extra in incomplete[:3]:
        console.print(f"    {rich_install_command(extra)}")
    if len(incomplete) > 3:
        console.print(f"    [dim]… and {len(incomplete) - 3} more listed above[/dim]")
    console.print(
        "  [dim]Developing on the repo? `uv sync --all-extras` installs every one.[/dim]"
    )


def _legacy_token_count() -> int:
    """Cleartext serve-token records still on disk, or 0 if unreadable.

    Never raises: `nova doctor` reporting nothing because a diagnostic threw is
    the failure mode this whole check exists to avoid.
    """
    try:
        from novafabric.serve.token_store import legacy_plaintext_count

        return legacy_plaintext_count()
    except Exception:  # noqa: BLE001 — a diagnostic must not break the command
        return 0


def _print_legacy_token_report(*, explicit: bool) -> None:
    """Report serve tokens whose secret is still stored in the clear (ADR-0252).

    ADR-0252 stopped writing the secret itself; it could not rewrite records
    already on disk. Without this report an operator has no way to learn that
    those records are still there, which would leave the migration half-done and
    silent — the token store would be *newly* correct and *historically* leaky
    with nothing saying so.
    """
    count = _legacy_token_count()
    if count == 0:
        if explicit:
            console.print()
            console.print("[bold]Serve tokens[/bold]")
            console.print("[green]OK[/green] — no token secret is stored in cleartext.")
        return
    console.print()
    console.print("[bold]Serve tokens[/bold]")
    console.print(
        f"[red]{count} token record(s) still store the secret in cleartext[/red] "
        "(pre-ADR-0252 records; newly issued tokens store only a fingerprint)."
    )
    console.print()
    console.print("[bold]Remediation[/bold]")
    console.print("  - revoke each affected token and issue a replacement")
    console.print("  - `nova serve token list` shows them; `... token revoke <fingerprint>`")
