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
    ] = Path.home() / ".novafabric" / "registry.db",
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

    One-time, idempotent migration. Requires novafabric[server] and
    NOVAFABRIC_DB_URL pointing to the target Postgres database.

    Scope: registry-wide.

    \b
    Examples:
      NOVAFABRIC_DB_URL=postgresql://user:pass@localhost/nova nova migrate-to-postgres
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
    """Replace the password in a DSN with ``***`` for display."""
    # Simple regex-free redaction: postgresql://user:pass@host/db
    if "://" in dsn and "@" in dsn:
        scheme_rest = dsn.split("://", 1)
        if len(scheme_rest) == 2:
            user_pass, rest = (
                scheme_rest[1].split("@", 1)
                if "@" in scheme_rest[1]
                else (scheme_rest[1], "")
            )
            if ":" in user_pass:
                user, _ = user_pass.split(":", 1)
                return f"{scheme_rest[0]}://{user}:***@{rest}"
    return dsn


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
