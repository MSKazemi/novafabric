from __future__ import annotations

from typing import Any

from novafabric.storage._base import StorageInfo

_KNOWN_TABLES = (
    "assets",
    "eval_results",
    "lineage_nodes",
    "lineage_edges",
)


class PostgresBackend:
    """Postgres storage backend.

    Requires ``novafabric[server]``: ``pip install 'novafabric[server]'``.
    psycopg3 is LGPL-3.0 (Tier B under ADR-0024); used only in this optional group.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def info(self) -> StorageInfo:
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "Postgres backend requires 'novafabric[server]'. "
                "Run: pip install 'novafabric[server]'"
            ) from exc

        with psycopg.connect(self._dsn) as conn:
            schema_version = _read_schema_version(conn)
            row_counts = _read_row_counts(conn)

        return StorageInfo(
            backend="postgres",
            db_path=None,
            schema_version=schema_version,
            row_counts=row_counts,
            migration_pending=_check_migration_pending(self._dsn),
        )


def _check_migration_pending(dsn: str) -> bool | None:
    """Registry postgres-track head comparison via the shared comparator (ADR-0211).

    Previously hardcoded ``None`` ("not determinable"); the comparator makes
    the answer real. Same mapping as the SQLite backend: True = behind head or
    never stamped; False = at head; None = unknown / foreign stamp.
    """
    from novafabric.server import schema_skew  # noqa: PLC0415

    status = schema_skew.compare_revisions("postgres", dsn).status
    if status == schema_skew.STATUS_OK:
        return False
    if status in (schema_skew.STATUS_BEHIND, schema_skew.STATUS_UNSTAMPED):
        return True
    return None


def _read_schema_version(conn: Any) -> int | None:
    try:
        row: Any = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        if row and row[0] is not None:
            return int(row[0])
        return None
    except Exception:  # noqa: BLE001
        return None


def _read_row_counts(conn: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in _KNOWN_TABLES:
        try:
            row: Any = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
            if row:
                counts[table] = int(row[0])
        except Exception:  # noqa: BLE001
            pass
    return counts
