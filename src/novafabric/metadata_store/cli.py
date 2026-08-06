"""CLI commands for the NovaFabric MetadataStore tier.

Commands exposed here are mounted under the ``nova db`` sub-app in main.py:

  nova db migrate-to-postgres   — idempotent SQLite → Postgres row migration
  nova db upgrade               — run ``alembic upgrade head`` for the active backend

Exit codes
----------
0 — success
1 — row-count verification failure
2 — connection / import / configuration error
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
from pathlib import Path
from typing import Annotated, Any, Optional

import typer
from rich.console import Console
from rich.table import Table

console = Console()

# ---------------------------------------------------------------------------
# Typer sub-app (mounted as `nova db` in main.py)
# ---------------------------------------------------------------------------

metadata_db_app = typer.Typer(
    name="db",
    help="MetadataStore management commands (migrate, upgrade, health).",
    no_args_is_help=True,
)

# Tables migrated by the tool.  Order matters for potential FK constraints.
_TABLES = ("runs", "capsules", "signatures", "retention_policies")


# ---------------------------------------------------------------------------
# migrate-to-postgres
# ---------------------------------------------------------------------------


@metadata_db_app.command("migrate-to-postgres")
def migrate_to_postgres_cmd(
    source: Annotated[
        Path,
        typer.Option(
            "--source",
            help="Path to the source SQLite metadata.db file (must exist).",
            show_default=False,
        ),
    ],
    target: Annotated[
        str,
        typer.Option(
            "--target",
            help="Postgres DSN (e.g. postgresql://user:pass@host/db).",
            show_default=False,
        ),
    ],
    batch_size: Annotated[
        int,
        typer.Option(
            "--batch-size",
            help="Number of rows per INSERT batch.",
        ),
    ] = 1000,
    report: Annotated[
        Optional[Path],
        typer.Option(
            "--report",
            help="Write a JSON migration report to this path.",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Idempotent migration of MetadataStore tables from SQLite to Postgres.

    The source SQLite file is never deleted.  Running the command multiple
    times is safe — all inserts use ON CONFLICT DO NOTHING.

    Exit code 0 → success (row counts verified).
    Exit code 1 → verification failure (Postgres count < SQLite count).
    Exit code 2 → connection / import error.
    """
    # --- validate source -------------------------------------------------
    if not source.exists():
        console.print(f"[red]Error:[/red] source database not found: {source}")
        raise typer.Exit(code=2)

    console.print(
        f"[bold]MetadataStore migration[/bold] "
        f"[cyan]{source}[/cyan] → [cyan]{_redact_dsn(target)}[/cyan]"
    )

    # --- import psycopg --------------------------------------------------
    try:
        import psycopg  # noqa: PLC0415
    except ImportError:
        console.print(
            "[red]Error:[/red] psycopg is not installed. "
            "Install novafabric[server]: pip install novafabric[server]"
        )
        raise typer.Exit(code=2) from None

    # --- open connections ------------------------------------------------
    try:
        sqlite_conn = sqlite3.connect(source)
        sqlite_conn.row_factory = sqlite3.Row
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Error:[/red] cannot open SQLite source: {exc}")
        raise typer.Exit(code=2) from exc

    try:
        pg_conn = psycopg.connect(target)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Error:[/red] cannot connect to Postgres: {exc}")
        sqlite_conn.close()
        raise typer.Exit(code=2) from exc

    # --- bootstrap Postgres schema ---------------------------------------
    try:
        _ensure_pg_schema(pg_conn)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Error:[/red] schema bootstrap failed: {exc}")
        sqlite_conn.close()
        pg_conn.close()
        raise typer.Exit(code=2) from exc

    # --- migrate tables --------------------------------------------------
    summary: dict[str, dict[str, int]] = {}
    verification_passed = True

    for table in _TABLES:
        result = _migrate_table(
            sqlite_conn=sqlite_conn,
            pg_conn=pg_conn,
            table=table,
            batch_size=batch_size,
        )
        summary[table] = result

        if result["skipped"]:
            console.print(f"  [dim]{table}:[/dim] table not found in SQLite — skipped")
            continue

        sqlite_count = result["sqlite_rows"]
        pg_count = result["pg_rows"]
        written = result["written"]

        console.print(
            f"  [green]{table}:[/green] "
            f"SQLite={sqlite_count}  written={written}  Postgres={pg_count}"
        )

        # Verification: Postgres must have at least as many rows as SQLite
        if pg_count < sqlite_count:
            console.print(
                f"  [red]FAIL[/red] {table}: "
                f"Postgres ({pg_count}) < SQLite ({sqlite_count})"
            )
            verification_passed = False

        # 1 % checksum sample
        if sqlite_count > 0:
            _checksum_sample(sqlite_conn, pg_conn, table)

    sqlite_conn.close()
    pg_conn.close()

    # --- report ----------------------------------------------------------
    _print_summary_table(summary)

    if report is not None:
        report_data = {
            "verification_passed": verification_passed,
            "tables": summary,
        }
        report.write_text(json.dumps(report_data, indent=2))
        console.print(f"\n[dim]Report written to:[/dim] {report}")

    if verification_passed:
        console.print("\n[bold green]PASS[/bold green] — row counts verified.")
    else:
        console.print(
            "\n[bold red]FAIL[/bold red] — row count mismatch detected. "
            "Check the output above."
        )
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# upgrade (nova db upgrade → alembic upgrade head)
# ---------------------------------------------------------------------------


def _alembic_ini_path(backend: str) -> Path | None:
    """Locate the packaged alembic.ini for *backend*, or None."""
    from importlib.resources import files  # noqa: PLC0415

    migrations_pkg = f"novafabric.metadata_store.migrations.{backend}"
    try:
        ini_path = Path(str(files(migrations_pkg).joinpath("alembic.ini")))
    except Exception:  # noqa: BLE001
        ini_path = Path(__file__).parent / "migrations" / backend / "alembic.ini"
    return ini_path if ini_path.exists() else None


def run_alembic_upgrade(
    backend: str, *, dsn: str | None = None, revision: str = "head"
) -> tuple[bool, str]:
    """Programmatic ``alembic upgrade`` (ADR-0217 restore step shares this).

    Unlike the Typer command, output is captured and returned; the DSN is
    passed to the child via env only and scrubbed from any surfaced text.
    """
    import subprocess  # noqa: PLC0415

    backend = backend.lower()
    if backend not in ("sqlite", "postgres"):
        return False, f"unknown backend {backend!r} — choose 'sqlite' or 'postgres'"
    ini_path = _alembic_ini_path(backend)
    if ini_path is None:
        return False, f"alembic.ini not found for backend {backend!r}"

    env = os.environ.copy()
    if backend == "postgres":
        resolved = dsn or env.get("NOVAFABRIC_METADATA_DSN", "")
        if not resolved:
            return False, (
                "no Postgres DSN — pass one or set NOVAFABRIC_METADATA_DSN"
            )
        env["NOVAFABRIC_METADATA_DSN"] = resolved
        scrub = resolved
    else:
        env.setdefault(
            "NOVAFABRIC_DB_PATH", str(Path.home() / ".novafabric" / "metadata.db")
        )
        scrub = None

    proc = subprocess.run(  # noqa: S603 — fixed binary, no shell
        ["alembic", "-c", str(ini_path), "upgrade", revision],
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "no output").strip()
        if scrub:
            detail = detail.replace(scrub, "<dsn redacted>")
        return False, f"alembic exited {proc.returncode}: {detail[:500]}"
    return True, f"alembic upgrade {revision} complete (backend={backend})"


@metadata_db_app.command("upgrade")
def db_upgrade_cmd(
    backend: Annotated[
        str,
        typer.Option(
            "--backend",
            help="Target backend: 'sqlite' or 'postgres'.",
        ),
    ] = "sqlite",
    revision: Annotated[
        str,
        typer.Option(
            "--revision",
            help="Alembic revision target (default: 'head'). "
                 "Use a specific revision ID (e.g. 'v001') to stop before gated migrations.",
        ),
    ] = "head",
    track: Annotated[
        str,
        typer.Option(
            "--track",
            help="Migration track: 'metadata' (MetadataStore tier, default — "
            "unchanged behavior) or 'registry' (the registry/server DB the "
            "server lifespan opens and `nova backup --profile pg` dumps; "
            "ADR-0211 D5).",
        ),
    ] = "metadata",
) -> None:
    """Run ``alembic upgrade <revision>`` for the selected migration track.

    Two parallel alembic tracks exist (ADR-0211): ``--track metadata``
    (default) migrates the MetadataStore tier — sqlite reads
    NOVAFABRIC_DB_PATH (default ~/.novafabric/metadata.db), postgres reads
    NOVAFABRIC_METADATA_DSN. ``--track registry`` (experimental) migrates the
    registry/server database — sqlite uses the registry DB path
    (~/.novafabric/registry.db), postgres reads NOVAFABRIC_POSTGRES_DSN. The
    server's schema-skew guard names the registry track.
    """
    import subprocess  # noqa: PLC0415
    from importlib.resources import files  # noqa: PLC0415

    backend = backend.lower()
    if backend not in ("sqlite", "postgres"):
        console.print(
            f"[red]Error:[/red] unknown backend '{backend}'. "
            "Choose 'sqlite' or 'postgres'."
        )
        raise typer.Exit(code=2)

    track = track.lower()
    if track not in ("metadata", "registry"):
        console.print(
            f"[red]Error:[/red] unknown track '{track}'. "
            "Choose 'metadata' or 'registry'."
        )
        raise typer.Exit(code=2)
    if track == "registry":
        _upgrade_registry_track(backend, revision)
        return

    # Locate the alembic.ini for the chosen backend
    migrations_pkg = (
        f"novafabric.metadata_store.migrations.{backend}"
    )
    try:
        alembic_ini = files(migrations_pkg).joinpath("alembic.ini")
        ini_path = str(alembic_ini)
    except Exception:  # noqa: BLE001
        # Fallback: resolve from __file__
        here = Path(__file__).parent
        ini_path = str(here / "migrations" / backend / "alembic.ini")

    if not Path(ini_path).exists():
        console.print(
            f"[red]Error:[/red] alembic.ini not found at {ini_path}"
        )
        raise typer.Exit(code=2)

    console.print(
        f"[bold]Running alembic upgrade {revision}[/bold] "
        f"(backend=[cyan]{backend}[/cyan])"
    )
    console.print(f"  config: [dim]{ini_path}[/dim]")

    # Build env for the subprocess
    env = os.environ.copy()
    if backend == "postgres":
        dsn = env.get("NOVAFABRIC_METADATA_DSN", "")
        if not dsn:
            console.print(
                "[red]Error:[/red] NOVAFABRIC_METADATA_DSN is not set. "
                "Export it before running upgrade against postgres."
            )
            raise typer.Exit(code=2)
    else:
        db_path = env.get(
            "NOVAFABRIC_DB_PATH",
            str(Path.home() / ".novafabric" / "metadata.db"),
        )
        env["NOVAFABRIC_DB_PATH"] = db_path
        console.print(f"  db_path: [dim]{db_path}[/dim]")

    proc = subprocess.run(  # noqa: S603
        ["alembic", "-c", ini_path, "upgrade", revision],
        env=env,
        capture_output=False,
    )

    if proc.returncode != 0:
        console.print(
            f"[red]Error:[/red] alembic exited with code {proc.returncode}"
        )
        raise typer.Exit(code=proc.returncode)

    console.print(f"[bold green]upgrade {revision} complete.[/bold green]")


def _upgrade_registry_track(backend: str, revision: str) -> None:
    """``nova db upgrade --track registry`` (ADR-0211 D5, experimental).

    Programmatic alembic against the registry-track trees (packaged in wheels,
    source checkout in dev) — works from a non-checkout install. The DSN is
    never printed.
    """
    from novafabric.migrations.registry_track import (  # noqa: PLC0415
        RegistryMigrationsUnavailableError,
        registry_alembic_config,
    )

    if backend == "postgres":
        dsn = os.environ.get("NOVAFABRIC_POSTGRES_DSN", "")
        if not dsn:
            console.print(
                "[red]Error:[/red] NOVAFABRIC_POSTGRES_DSN is not set. "
                "Export it before running --track registry against postgres."
            )
            raise typer.Exit(code=2)
        url = dsn
        target_desc = "postgres (NOVAFABRIC_POSTGRES_DSN)"
    else:
        from novafabric.registry.store import get_db_path  # noqa: PLC0415

        db_path = get_db_path()
        url = f"sqlite:///{db_path}"
        target_desc = str(db_path)

    console.print(
        f"[bold]Running alembic upgrade {revision}[/bold] "
        f"(track=[cyan]registry[/cyan], backend=[cyan]{backend}[/cyan])"
    )
    console.print(f"  target: [dim]{target_desc}[/dim]")

    try:
        from alembic import command  # noqa: PLC0415

        cfg = registry_alembic_config(backend, url=url)
        command.upgrade(cfg, revision)
    except RegistryMigrationsUnavailableError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except Exception as exc:  # noqa: BLE001 — alembic/db failure; no DSN echo
        console.print(
            f"[red]Error:[/red] alembic upgrade failed ({type(exc).__name__})"
        )
        raise typer.Exit(code=1) from exc

    console.print(
        f"[bold green]upgrade {revision} complete (registry track).[/bold green]"
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_PG_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT,
    tenant_id         TEXT,
    event_type        TEXT,
    global_run_id     TEXT,
    started_at        TEXT,
    status            TEXT DEFAULT 'pending',
    world_size        BIGINT,
    expected_children BIGINT,
    children_arrived  BIGINT DEFAULT 0,
    PRIMARY KEY (run_id, tenant_id)
);
CREATE TABLE IF NOT EXISTS capsules (
    capsule_uri TEXT PRIMARY KEY,
    run_id      TEXT,
    tenant_id   TEXT
);
CREATE TABLE IF NOT EXISTS signatures (
    run_id         TEXT,
    tenant_id      TEXT,
    signature_hash TEXT,
    payload_json   TEXT,
    PRIMARY KEY (run_id, signature_hash)
);
CREATE TABLE IF NOT EXISTS retention_policies (
    tenant_id   TEXT PRIMARY KEY,
    policy_json TEXT
);
"""


def _ensure_pg_schema(pg_conn: Any) -> None:
    """Create tables in Postgres if they don't already exist (no RLS)."""
    # Execute each CREATE TABLE separately — psycopg can't run semicolon-separated batches
    statements = [s.strip() for s in _PG_DDL.split(";") if s.strip()]
    with pg_conn.transaction():
        for stmt in statements:
            pg_conn.execute(stmt)


def _migrate_table(
    *,
    sqlite_conn: sqlite3.Connection,
    pg_conn: Any,
    table: str,
    batch_size: int,
) -> dict[str, int]:
    """Migrate a single table from SQLite to Postgres.

    Returns a dict with keys:
      skipped      (1 if table absent in SQLite, else 0)
      sqlite_rows  (row count in SQLite)
      written      (rows inserted into Postgres this run)
      pg_rows      (row count in Postgres after migration)
    """
    # Check if table exists in SQLite
    exists = sqlite_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if exists is None:
        return {"skipped": 1, "sqlite_rows": 0, "written": 0, "pg_rows": 0}

    # Fetch all rows from SQLite
    cursor = sqlite_conn.execute(f"SELECT * FROM {table}")  # noqa: S608
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    sqlite_count = len(rows)

    if sqlite_count == 0:
        pg_count = pg_conn.execute(              f"SELECT COUNT(*) FROM {table}"  # noqa: S608
        ).fetchone()[0]
        return {"skipped": 0, "sqlite_rows": 0, "written": 0, "pg_rows": pg_count}

    # Build ON CONFLICT DO NOTHING INSERT
    placeholders = ", ".join(["%s"] * len(columns))
    col_names = ", ".join(columns)

    # Determine conflict target based on table
    conflict_targets: dict[str, str] = {
        # Must match the primary key exactly — Postgres infers the arbiter index
        # from these columns. `runs` is range-partitioned on `started_at`
        # (ADR-0051), so the partition column is part of the key and has to be
        # named here too; RFC-0001 widened it to three columns. A stale target
        # here fails with `InvalidColumnReference` at INSERT time, not at parse
        # time, so it surfaces as a migration that dies partway through.
        "runs": "(run_id, tenant_id, started_at)",
        "capsules": "(capsule_uri)",
        "signatures": "(run_id, signature_hash)",
        "retention_policies": "(tenant_id)",
    }
    conflict_target = conflict_targets.get(table, "")
    if conflict_target:
        on_conflict = f"ON CONFLICT {conflict_target} DO NOTHING"
    else:
        on_conflict = ""

    sql = (
        f"INSERT INTO {table} ({col_names}) VALUES ({placeholders}) {on_conflict}"  # noqa: S608
    )

    total_written = 0
    # Batch insert
    for i in range(0, sqlite_count, batch_size):
        batch = [tuple(row) for row in rows[i : i + batch_size]]
        with pg_conn.transaction():
            for row_vals in batch:
                pg_conn.execute(sql, row_vals)
        total_written += len(batch)

    pg_count = pg_conn.execute(          f"SELECT COUNT(*) FROM {table}"  # noqa: S608
    ).fetchone()[0]

    return {
        "skipped": 0,
        "sqlite_rows": sqlite_count,
        "written": total_written,
        "pg_rows": pg_count,
    }


def _checksum_sample(
    sqlite_conn: sqlite3.Connection,
    pg_conn: Any,
    table: str,
) -> None:
    """Pick 1% of rows (min 1) from SQLite and verify they exist in Postgres by PK."""
    pks: dict[str, tuple[str, ...]] = {
        "runs": ("run_id", "tenant_id"),
        "capsules": ("capsule_uri",),
        "signatures": ("run_id", "signature_hash"),
        "retention_policies": ("tenant_id",),
    }
    pk_cols = pks.get(table)
    if not pk_cols:
        return

    all_rows = sqlite_conn.execute(
        f"SELECT {', '.join(pk_cols)} FROM {table}"  # noqa: S608
    ).fetchall()
    if not all_rows:
        return

    sample_size = max(1, len(all_rows) // 100)
    sample = random.sample(all_rows, min(sample_size, len(all_rows)))

    where_clause = " AND ".join([f"{col} = %s" for col in pk_cols])
    for row in sample:
        result = pg_conn.execute(              f"SELECT 1 FROM {table} WHERE {where_clause}",  # noqa: S608
            tuple(str(v) for v in row),
        ).fetchone()
        if result is None:
            console.print(
                f"  [yellow]WARNING[/yellow] checksum sample miss in {table}: "
                f"PK={row!r} not found in Postgres"
            )


def _print_summary_table(summary: dict[str, dict[str, int]]) -> None:
    """Print a Rich table summarising migration results per table."""
    tbl = Table(show_header=True, header_style="bold")
    tbl.add_column("Table")
    tbl.add_column("SQLite rows", justify="right")
    tbl.add_column("Written", justify="right")
    tbl.add_column("Postgres total", justify="right")
    tbl.add_column("Status")

    for table, result in summary.items():
        if result["skipped"]:
            tbl.add_row(table, "—", "—", "—", "[dim]skipped[/dim]")
        else:
            ok = result["pg_rows"] >= result["sqlite_rows"]
            status = "[green]ok[/green]" if ok else "[red]mismatch[/red]"
            tbl.add_row(
                table,
                str(result["sqlite_rows"]),
                str(result["written"]),
                str(result["pg_rows"]),
                status,
            )

    console.print()
    console.print(tbl)


def _redact_dsn(dsn: str) -> str:
    """Replace password in DSN with *** for display."""
    if "://" in dsn and "@" in dsn:
        scheme, rest = dsn.split("://", 1)
        if "@" in rest:
            user_pass, host_part = rest.split("@", 1)
            if ":" in user_pass:
                user, _ = user_pass.split(":", 1)
                return f"{scheme}://{user}:***@{host_part}"
    return dsn
