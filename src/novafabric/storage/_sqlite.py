from __future__ import annotations

import sqlite3
from pathlib import Path

from novafabric.registry.store import get_connection, get_db_path
from novafabric.storage._base import StorageInfo

_KNOWN_TABLES = (
    "assets",
    "eval_results",
    "lineage_nodes",
    "lineage_edges",
)


class SQLiteBackend:
    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or get_db_path()

    def info(self) -> StorageInfo:
        if not self._db_path.exists():
            return StorageInfo(
                backend="sqlite",
                db_path=str(self._db_path),
                schema_version=None,
                row_counts={},
                migration_pending=None,
            )

        conn = get_connection(self._db_path)
        try:
            schema_version = _read_schema_version(conn)
            row_counts = _read_row_counts(conn)
            migration_pending = _check_migration_pending(conn)
        finally:
            conn.close()

        return StorageInfo(
            backend="sqlite",
            db_path=str(self._db_path),
            schema_version=schema_version,
            row_counts=row_counts,
            migration_pending=migration_pending,
        )


def _read_schema_version(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        if row and row[0] is not None:
            return int(row[0])
        return None
    except sqlite3.OperationalError:
        return None


def _read_row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in _KNOWN_TABLES:
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
            if row:
                counts[table] = int(row[0])
        except sqlite3.OperationalError:
            pass
    return counts


def _check_migration_pending(conn: sqlite3.Connection) -> bool | None:
    try:
        import alembic  # noqa: F401  # type: ignore[import-untyped]
    except ImportError:
        return None  # alembic not installed; skip check

    try:
        row = conn.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
        # alembic_version table exists; assume current (head comparison added later)
        return row is None  # table exists but empty means no migration applied
    except sqlite3.OperationalError:
        return True  # alembic_version table absent; migrations not yet applied
