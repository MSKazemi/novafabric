"""CLI command: ``nova migrate-to-postgres``.

One-time, idempotent migration from a local SQLite database to Postgres.

Exit codes
----------
0 — success (all row counts verified)
1 — verification failure (Postgres row count lower than SQLite)
2 — connection / import error
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from novafabric import _paths as _nf_paths
from novafabric._dsn import redact_dsn
from novafabric.storage._migration import MigrationSummary, run_migration

console = Console()


def migrate_to_postgres_cmd(
    source: Annotated[
        Path,
        typer.Option(
            "--source",
            help=(
                "Path to the source SQLite database "
                "(default: ~/.novafabric/registry.db)."
            ),
            show_default=False,
        ),
    ] = _nf_paths.nova_home() / "registry.db",
    target: Annotated[
        Optional[str],
        typer.Option(
            "--target",
            help="Postgres DSN (e.g. postgresql://user:pass@host/db). "
            "Overrides NOVAFABRIC_POSTGRES_DSN.",
            show_default=False,
            envvar="NOVAFABRIC_POSTGRES_DSN",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print what would be migrated without writing to Postgres.",
        ),
    ] = False,
    log: Annotated[
        Optional[Path],
        typer.Option(
            "--log",
            help="Optional path for a JSONL migration log (one record per table).",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Migrate a local SQLite registry to Postgres.

    One-time, idempotent migration. Requires the 'novafabric\\[server]' extra
    and a target Postgres DSN: --target, or NOVAFABRIC_POSTGRES_DSN.

    Scope: registry-wide.

    \b
    Examples:
      nova migrate-to-postgres --target postgresql://user:pass@localhost/nova
      NOVAFABRIC_POSTGRES_DSN=postgresql://user:pass@localhost/nova nova migrate-to-postgres
    """
    if not dry_run and not target:
        console.print(
            "[red]Error:[/red] --target <postgres-dsn> is required "
            "(or set NOVAFABRIC_POSTGRES_DSN)."
        )
        raise typer.Exit(code=2)

    if not source.exists():
        console.print(f"[red]Error:[/red] Source database not found: {source}")
        raise typer.Exit(code=2)

    if dry_run:
        console.print(
            f"[bold]Dry run[/bold] — reading from [cyan]{source}[/cyan] "
            "(no writes to Postgres)"
        )
    else:
        console.print(
            f"[bold]Migrating[/bold] [cyan]{source}[/cyan] → "
            f"[cyan]{_redact_dsn(target or '')}[/cyan]"
        )

    try:
        summary = run_migration(
            source=source,
            target_dsn=target or "",
            dry_run=dry_run,
            log_path=log,
        )
    except ConnectionError as exc:
        console.print(f"[red]Connection error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except RuntimeError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    _print_summary(summary)

    if log is not None:
        console.print(f"\n[dim]Migration log written to:[/dim] {log}")

    raise typer.Exit(code=summary.exit_code())


# ── Formatting helpers ────────────────────────────────────────────────────────


def _redact_dsn(dsn: str) -> str:
    """``host/dbname`` for display — never a user or password (review storage-backup-10)."""
    return redact_dsn(dsn)


def _print_summary(summary: MigrationSummary) -> None:
    console.print()

    # Per-table row counts
    tbl = Table(show_header=True, header_style="bold")
    tbl.add_column("Table")
    tbl.add_column("SQLite rows", justify="right")
    if not summary.dry_run:
        tbl.add_column("Written", justify="right")
        tbl.add_column("Postgres total", justify="right")

    for result in summary.table_results:
        sqlite_rows = str(summary.sqlite_counts.get(result.table, result.rows_read))
        if summary.dry_run:
            tbl.add_row(result.table, sqlite_rows)
        else:
            pg_total = str(summary.postgres_counts.get(result.table, "—"))
            tbl.add_row(
                result.table,
                sqlite_rows,
                str(result.rows_written),
                pg_total,
            )

    console.print(tbl)

    if summary.not_migrated:
        left = ", ".join(f"{t} ({n} rows)" for t, n in summary.not_migrated.items())
        console.print(
            f"\n[red]✗ Tables not migrated:[/red] {left}. They have no Postgres "
            "counterpart in this migration, so their rows would be lost — "
            "the migration is not complete (exit 1).",
            soft_wrap=True,
        )

    if summary.dry_run:
        console.print(
            "\n[yellow]Dry run complete.[/yellow] "
            "Re-run without --dry-run to write."
        )
        return

    console.print()
    if summary.verification_passed:
        console.print("[bold green]PASS[/bold green] — row counts verified.")
    else:
        console.print(
            "[bold red]FAIL[/bold red] — row count mismatch. "
            "Postgres has fewer rows than SQLite for one or more tables."
        )
        _print_mismatch_detail(summary)


def _print_mismatch_detail(summary: MigrationSummary) -> None:
    for table, sqlite_count in summary.sqlite_counts.items():
        pg_count = summary.postgres_counts.get(table, 0)
        if pg_count < sqlite_count:
            console.print(
                f"  [red]{table}[/red]: SQLite={sqlite_count} "
                f"Postgres={pg_count}"
            )
