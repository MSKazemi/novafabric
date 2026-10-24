"""SQLite-to-Postgres migration logic for ``nova migrate-to-postgres``.

Reads all rows from the registry tables in a source SQLite database (assets,
eval results, the lineage index, and the append-only ``asset_status_history``
that ``nova rollback`` reads) and writes them into a target Postgres database
via psycopg3 using ``INSERT … ON CONFLICT DO NOTHING`` (idempotent upsert
semantics).

Design constraints
------------------
- No ORM: raw SQL via ``sqlite3`` (source) and ``psycopg3`` (target).
- Source SQLite is never modified or deleted.
- Migration is idempotent: a second run after a partial run adds only the
  missing rows.
- ``--dry-run`` reads from SQLite and reports what would be written without
  opening a Postgres connection.

Exit-code conventions (used by the CLI layer)
---------------------------------------------
0 — success (all row counts verified)
1 — verification failure (row counts do not match)
2 — connection / import error
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Column definitions per table ─────────────────────────────────────────────
# The order must match the SELECT order used in _read_table().

_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "assets": (
        "id",
        "name",
        "asset_type",
        "version",
        "status",
        "spec_json",
        "git_commit_sha",
        "created_at",
        "promoted_at",
        "promoted_by",
        "forced_promotion",
    ),
    "eval_results": (
        "id",
        "asset_id",
        "suite_name",
        "passed",
        "score_json",
        "run_at",
    ),
    "lineage_nodes": (
        "node_id",
        "kind",
        "ref",
        "first_seen_capsule_run_id",
        "payload",
    ),
    "lineage_edges": (
        "edge_id",
        "edge_type",
        "source_id",
        "target_id",
        "capsule_run_id",
        "confidence",
        "created_at",
        "payload",
    ),
    # Append-only lifecycle history (registry/store.py). The SQLite row ids are
    # copied as-is: they are the conflict key that makes a re-run add nothing,
    # and they carry the order of the transitions.
    "asset_status_history": (
        "id",
        "asset_name",
        "version",
        "from_status",
        "to_status",
        "changed_at",
        "changed_by",
        "via",
        "asset_id",
        "spec_sha256",
    ),
}

# Nullable columns added to a table after it first shipped. A source registry
# from before the column existed is read with NULL in its place: the source is
# opened read-only, so the registry's own ALTER TABLE never runs on it.
_ADDITIVE_NULLABLE_COLUMNS: dict[str, frozenset[str]] = {
    "asset_status_history": frozenset({"asset_id", "spec_sha256"}),
}

# Rows are read in this order, so the target receives them in the source's order.
_TABLE_ORDER_BY: dict[str, str] = {
    "asset_status_history": "id",
}

# Tables that must be inserted *before* tables with FK dependencies.
_TABLE_ORDER = (
    "assets",
    "eval_results",
    "lineage_nodes",
    "lineage_edges",
    "asset_status_history",
)

#: Source tables that are deliberately not migrated, and why. Every *other*
#: table holding rows fails the migration (review storage-backup-11: label
#: protections, maker-checker approvals and comments were silently left behind
#: while the run reported "verified").
_NOT_MIGRATED_BY_DESIGN: dict[str, str] = {
    "runs_cache": "a derived index, rebuilt from the capsules",
    "alembic_version": "schema bookkeeping of the source database",
    "schema_version": "the source registry's own schema bookkeeping",
}


# ── Result types ─────────────────────────────────────────────────────────────


@dataclass
class TableMigrationResult:
    table: str
    rows_read: int
    rows_written: int
    status: str  # "ok" | "skipped" | "error"
    error: str | None = None

    def as_jsonl_record(self) -> str:
        record: dict[str, Any] = {
            "table": self.table,
            "rows_read": self.rows_read,
            "rows_written": self.rows_written,
            "status": self.status,
        }
        if self.error is not None:
            record["error"] = self.error
        return json.dumps(record)


@dataclass
class MigrationSummary:
    dry_run: bool
    table_results: list[TableMigrationResult] = field(default_factory=list)
    # sqlite_counts[table] = count read from SQLite
    sqlite_counts: dict[str, int] = field(default_factory=dict)
    # postgres_counts[table] = count confirmed in Postgres after write
    postgres_counts: dict[str, int] = field(default_factory=dict)
    verification_passed: bool | None = None  # None in dry-run mode
    # not_migrated[table] = rows in a source table this migration does not carry
    not_migrated: dict[str, int] = field(default_factory=dict)

    def exit_code(self) -> int:
        """Return the appropriate process exit code.

        1 whenever source rows would be (or were) left behind — a dry run too,
        since previewing that is what it is for — or the counts do not verify.
        """
        if self.not_migrated:
            return 1
        if self.dry_run:
            return 0
        if self.verification_passed is False:
            return 1
        return 0


# ── SQLite read helpers ───────────────────────────────────────────────────────


def _open_sqlite(source: Path) -> sqlite3.Connection:
    """Open a read-only connection to the source SQLite file."""
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _read_table(
    conn: sqlite3.Connection, table: str
) -> tuple[int, list[tuple[Any, ...]]]:
    """Return (row_count, rows) for *table*, or (0, []) if the table is absent."""
    if not _table_exists(conn, table):
        return 0, []
    present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    additive = _ADDITIVE_NULLABLE_COLUMNS.get(table, frozenset())
    cols = ", ".join(
        col if col in present or col not in additive else f"NULL AS {col}"
        for col in _TABLE_COLUMNS[table]
    )
    order_by = _TABLE_ORDER_BY.get(table)
    order = f" ORDER BY {order_by}" if order_by else ""
    rows_raw = conn.execute(
        f"SELECT {cols} FROM {table}{order}"  # noqa: S608
    ).fetchall()
    rows = [tuple(r) for r in rows_raw]
    return len(rows), rows


# ── Postgres write helpers ────────────────────────────────────────────────────


def _ensure_postgres_schema(pg_conn: Any, table: str) -> None:
    """Create the table if it does not exist (idempotent)."""
    ddl_map: dict[str, str] = {
        "assets": """
            CREATE TABLE IF NOT EXISTS assets (
                id               TEXT PRIMARY KEY,
                name             TEXT NOT NULL,
                asset_type       TEXT NOT NULL,
                version          TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'development',
                spec_json        TEXT NOT NULL,
                git_commit_sha   TEXT,
                created_at       TEXT NOT NULL,
                promoted_at      TEXT,
                promoted_by      TEXT,
                forced_promotion INTEGER NOT NULL DEFAULT 0,
                UNIQUE(name, version)
            )
        """,
        "eval_results": """
            CREATE TABLE IF NOT EXISTS eval_results (
                id         TEXT PRIMARY KEY,
                asset_id   TEXT NOT NULL,
                suite_name TEXT NOT NULL,
                passed     INTEGER NOT NULL,
                score_json TEXT,
                run_at     TEXT NOT NULL
            )
        """,
        "lineage_nodes": """
            CREATE TABLE IF NOT EXISTS lineage_nodes (
                node_id                   TEXT PRIMARY KEY,
                kind                      TEXT NOT NULL,
                ref                       TEXT NOT NULL,
                first_seen_capsule_run_id TEXT,
                payload                   TEXT NOT NULL,
                UNIQUE(kind, ref)
            )
        """,
        "lineage_edges": """
            CREATE TABLE IF NOT EXISTS lineage_edges (
                edge_id        TEXT PRIMARY KEY,
                edge_type      TEXT NOT NULL,
                source_id      TEXT NOT NULL,
                target_id      TEXT NOT NULL,
                capsule_run_id TEXT NOT NULL,
                confidence     TEXT,
                created_at     TEXT NOT NULL,
                payload        TEXT NOT NULL
            )
        """,
        # No identity default: every row arrives with its SQLite id, and that
        # id is the ON CONFLICT key. asset_id/spec_sha256 are NULL on rows
        # written before the registration binding existed.
        "asset_status_history": """
            CREATE TABLE IF NOT EXISTS asset_status_history (
                id          BIGINT PRIMARY KEY,
                asset_name  TEXT NOT NULL,
                version     TEXT NOT NULL,
                from_status TEXT,
                to_status   TEXT NOT NULL,
                changed_at  TEXT NOT NULL,
                changed_by  TEXT NOT NULL,
                via         TEXT NOT NULL,
                asset_id    TEXT,
                spec_sha256 TEXT
            )
        """,
    }
    pg_conn.execute(ddl_map[table])


def _write_table(pg_conn: Any, table: str, rows: list[tuple[Any, ...]]) -> int:
    """Insert *rows* into *table* using ON CONFLICT DO NOTHING.

    Returns the number of rows actually inserted (i.e. not already present).
    """
    if not rows:
        return 0

    cols = _TABLE_COLUMNS[table]
    placeholders = ", ".join("%s" for _ in cols)
    col_list = ", ".join(cols)
    sql = (
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "  # noqa: S608
        "ON CONFLICT DO NOTHING"
    )
    written = 0
    for row in rows:
        cur = pg_conn.execute(sql, row)
        written += cur.rowcount
    return written


def _count_table(pg_conn: Any, table: str) -> int:
    """Return the row count for *table* in Postgres."""
    row = pg_conn.execute(
        f"SELECT COUNT(*) FROM {table}"  # noqa: S608
    ).fetchone()
    if row is None:
        return 0
    return int(row[0])


# ── Public API ────────────────────────────────────────────────────────────────


def run_migration(
    source: Path,
    target_dsn: str,
    *,
    dry_run: bool = False,
    log_path: Path | None = None,
) -> MigrationSummary:
    """Run the SQLite-to-Postgres migration.

    Parameters
    ----------
    source:
        Path to the source SQLite database (read-only; never modified).
    target_dsn:
        Postgres connection string.  Ignored when *dry_run* is ``True``.
    dry_run:
        If ``True``, read from SQLite and compute what would be written but do
        not open a Postgres connection.
    log_path:
        Optional path for the JSONL migration log (one record per table).

    Returns
    -------
    MigrationSummary
        Contains per-table results, row counts, and a verification verdict.
        Call ``.exit_code()`` to get the appropriate process exit code.
    """
    sqlite_conn = _open_sqlite(source)
    try:
        return _execute_migration(
            sqlite_conn=sqlite_conn,
            target_dsn=target_dsn,
            dry_run=dry_run,
            log_path=log_path,
        )
    finally:
        sqlite_conn.close()


def _execute_migration(
    sqlite_conn: sqlite3.Connection,
    target_dsn: str,
    *,
    dry_run: bool,
    log_path: Path | None,
) -> MigrationSummary:
    summary = MigrationSummary(dry_run=dry_run)

    summary.not_migrated = _tables_left_behind(sqlite_conn)

    # Phase 1: read from SQLite
    table_data: dict[str, tuple[int, list[tuple[Any, ...]]]] = {}
    for table in _TABLE_ORDER:
        count, rows = _read_table(sqlite_conn, table)
        table_data[table] = (count, rows)
        summary.sqlite_counts[table] = count

    if dry_run:
        for table in _TABLE_ORDER:
            count, rows = table_data[table]
            result = TableMigrationResult(
                table=table,
                rows_read=count,
                rows_written=0,
                status="skipped",
            )
            summary.table_results.append(result)
        _write_log(summary, log_path)
        return summary

    # Phase 2: write to Postgres
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "Postgres migration requires 'novafabric[server]'. "
            "Run: pip install 'novafabric[server]'"
        ) from exc

    try:
        pg_conn = psycopg.connect(target_dsn)
    except Exception as exc:
        raise ConnectionError(f"Cannot connect to Postgres: {exc}") from exc

    try:
        with pg_conn:
            for table in _TABLE_ORDER:
                count, rows = table_data[table]
                _ensure_postgres_schema(pg_conn, table)
                written = _write_table(pg_conn, table, rows)
                result = TableMigrationResult(
                    table=table,
                    rows_read=count,
                    rows_written=written,
                    status="ok",
                )
                summary.table_results.append(result)

            # Phase 3: verify row counts
            for table in _TABLE_ORDER:
                summary.postgres_counts[table] = _count_table(pg_conn, table)
    finally:
        pg_conn.close()

    # Verification: every Postgres count must be >= SQLite count.
    # (Postgres may have *more* rows if rows were added by other means; the
    # invariant is that nothing was lost.)
    all_ok = all(
        summary.postgres_counts.get(t, 0) >= summary.sqlite_counts.get(t, 0)
        for t in _TABLE_ORDER
    )
    summary.verification_passed = all_ok and not summary.not_migrated

    _write_log(summary, log_path)
    return summary


def _tables_left_behind(conn: sqlite3.Connection) -> dict[str, int]:
    """Source tables with rows that this migration does not carry, by row count."""
    names = [
        str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    left: dict[str, int] = {}
    for name in sorted(names):
        if name in _TABLE_ORDER or name in _NOT_MIGRATED_BY_DESIGN:
            continue
        quoted = '"' + name.replace('"', '""') + '"'
        (count,) = conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()  # noqa: S608
        if count:
            left[name] = int(count)
    return left


def _write_log(summary: MigrationSummary, log_path: Path | None) -> None:
    if log_path is None:
        return
    with log_path.open("a", encoding="utf-8") as fh:
        for result in summary.table_results:
            fh.write(result.as_jsonl_record() + "\n")
