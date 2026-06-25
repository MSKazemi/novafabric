"""SQLite-backed store for signed promotion policy bundles.

Uses the same SQLite database as MerkleLog (promote_policy table).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from novafabric.promote.exceptions import PolicyNotFoundError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS promote_policy (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace   TEXT    NOT NULL DEFAULT 'default',
    version     INTEGER NOT NULL,
    bundle_json TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);
"""


class PolicyStore:
    """Stores signed policy bundles in a SQLite database."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def put(self, bundle_json: str, namespace: str = "default") -> int:
        """Insert a new policy bundle and return its version number (= row id)."""
        created_at = datetime.now(UTC).isoformat()
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO promote_policy (namespace, version, bundle_json, created_at) "
                "VALUES (?, 0, ?, ?)",
                (namespace, bundle_json, created_at),
            )
            row_id = cursor.lastrowid
            # Update version to equal the row id so it's auto-assigned and stable
            self._conn.execute(
                "UPDATE promote_policy SET version = ? WHERE id = ?",
                (row_id, row_id),
            )
        return row_id  # type: ignore[return-value]

    def get_by_version(self, version: int, namespace: str = "default") -> str:
        """Return bundle_json for the given version; raises PolicyNotFoundError."""
        row = self._conn.execute(
            "SELECT bundle_json FROM promote_policy WHERE version = ? AND namespace = ?",
            (version, namespace),
        ).fetchone()
        if row is None:
            raise PolicyNotFoundError(
                f"No policy version {version} in namespace {namespace!r}"
            )
        return str(row[0])

    def get_active_at(self, timestamp: datetime, namespace: str = "default") -> str:
        """Return bundle_json for highest version where created_at <= timestamp."""
        ts_str = timestamp.isoformat()
        row = self._conn.execute(
            "SELECT bundle_json FROM promote_policy "
            "WHERE namespace = ? AND created_at <= ? "
            "ORDER BY version DESC LIMIT 1",
            (namespace, ts_str),
        ).fetchone()
        if row is None:
            raise PolicyNotFoundError(
                f"No policy active at {ts_str!r} in namespace {namespace!r}"
            )
        return str(row[0])

    def get_latest(self, namespace: str = "default") -> str:
        """Return bundle_json for the highest version number."""
        row = self._conn.execute(
            "SELECT bundle_json FROM promote_policy "
            "WHERE namespace = ? ORDER BY version DESC LIMIT 1",
            (namespace,),
        ).fetchone()
        if row is None:
            raise PolicyNotFoundError(
                f"No policy found in namespace {namespace!r}"
            )
        return str(row[0])

    def get_latest_version(self, namespace: str = "default") -> int:
        """Return the version number of the latest policy."""
        row = self._conn.execute(
            "SELECT version FROM promote_policy "
            "WHERE namespace = ? ORDER BY version DESC LIMIT 1",
            (namespace,),
        ).fetchone()
        if row is None:
            raise PolicyNotFoundError(
                f"No policy found in namespace {namespace!r}"
            )
        return int(row[0])

    def list_all(self, namespace: str | None = None) -> list[dict[str, object]]:
        """Return all policy rows as dicts (version, namespace, created_at).

        If *namespace* is None all namespaces are returned, ordered by
        namespace then version descending.
        """
        if namespace is not None:
            rows = self._conn.execute(
                "SELECT version, namespace, created_at FROM promote_policy "
                "WHERE namespace = ? ORDER BY version DESC",
                (namespace,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT version, namespace, created_at FROM promote_policy "
                "ORDER BY namespace, version DESC",
            ).fetchall()
        return [{"version": r[0], "namespace": r[1], "created_at": r[2]} for r in rows]
