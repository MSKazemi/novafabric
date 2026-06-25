"""SQLite-backed tracker for KG auto-ingest state.

Stores the set of capsule directory paths that have already been ingested so
that the auto-ingest loop survives `nova serve` restarts without re-processing
every capsule on each boot.  The underlying MERGE semantics in KuzuDB are
idempotent, so re-ingest is safe but wasteful at scale.

Schema (single table, no migrations required):
  CREATE TABLE IF NOT EXISTS ingest_state (
      capsule_path TEXT PRIMARY KEY,
      ingested_at  TEXT NOT NULL  -- ISO-8601 UTC
  )
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS ingest_state (
    capsule_path TEXT PRIMARY KEY,
    ingested_at  TEXT NOT NULL
);
"""


class IngestTracker:
    """Thread-safe SQLite-backed set of already-ingested capsule directory paths."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute(_CREATE_SQL)
        self._conn.commit()

    @contextmanager
    def _tx(self):  # type: ignore[no-untyped-def]
        """Yield the connection inside a transaction; commit on exit, rollback on error."""
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def contains(self, capsule_path: str) -> bool:
        """Return True if *capsule_path* was already successfully ingested."""
        row = self._conn.execute(
            "SELECT 1 FROM ingest_state WHERE capsule_path = ?", (capsule_path,)
        ).fetchone()
        return row is not None

    def mark(self, capsule_path: str) -> None:
        """Record that *capsule_path* has been ingested (idempotent INSERT OR REPLACE)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._tx():
            self._conn.execute(
                "INSERT OR REPLACE INTO ingest_state (capsule_path, ingested_at) VALUES (?, ?)",
                (capsule_path, now),
            )

    def all_paths(self) -> list[str]:
        """Return all tracked capsule paths (for diagnostics)."""
        rows = self._conn.execute("SELECT capsule_path FROM ingest_state").fetchall()
        return [r[0] for r in rows]

    def count(self) -> int:
        """Return the number of tracked capsule paths."""
        row = self._conn.execute("SELECT COUNT(*) FROM ingest_state").fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        """Close the SQLite connection."""
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass
