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
        finally:
            conn.close()
        migration_pending = _check_migration_pending(self._db_path)

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


def _check_migration_pending(db_path: Path) -> bool | None:
    """Registry-track head comparison via the shared comparator (ADR-0211 D1).

    True = migrations pending (behind head, or never stamped); False = at
    head; None = not determinable (unreadable DB, unresolvable head, or a
    foreign/ahead stamp this build cannot reason about).
    """
    from novafabric.server import schema_skew  # noqa: PLC0415

    status = schema_skew.compare_revisions("sqlite", db_path).status
    if status == schema_skew.STATUS_OK:
        return False
    if status in (schema_skew.STATUS_BEHIND, schema_skew.STATUS_UNSTAMPED):
        return True
    return None  # ahead_or_foreign / unknown — "pending" would misdescribe it
