# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Local WAL (Write-Ahead Log) for deferred NovaSeal signing (adr-002, FR-06).

Atomicity invariant (adr-002):
- WAL writes MUST be fsync-durable before ``put_capsule()`` returns
  ``PENDING_SIGNATURE``.
- On host restart, drain WAL idempotently before accepting new traffic.
- WAL entries contain ``(capsule_uri, capsule_sha256, tenant, run_id,
  uploaded_at)`` — NEVER capsule payload bytes.

Storage: SQLite in WAL journal mode (stdlib ``sqlite3``).  No external
dependency.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from novafabric.object_capsule_store.models import WalEntry

log = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS wal_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    capsule_uri   TEXT    NOT NULL,
    capsule_sha256 TEXT   NOT NULL,
    tenant        TEXT    NOT NULL,
    run_id        TEXT    NOT NULL,
    uploaded_at   TEXT    NOT NULL,
    -- 0 = pending, 1 = drain complete, 2 = poisoned (dead-lettered)
    drained       INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,  -- failed drain attempts
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_wal_undrained ON wal_entries (drained) WHERE drained = 0;
"""

# A WAL row that fails to drain this many times (for reasons other than a
# transient NovaSealUnavailable) is dead-lettered so it stops being retried
# forever on every drain cycle.
DEFAULT_MAX_WAL_ATTEMPTS = 5

# Terminal ``drained`` state for a poisoned/dead-lettered row.
_DRAINED_POISONED = 2


class LocalWal:
    """SQLite-backed WAL for unsigned-uploaded capsules.

    Thread-safe: uses ``check_same_thread=False`` with per-call commits.

    Args:
        db_path: Path to the SQLite database file.  If ``":memory:"`` is
                 given, an in-memory database is used (for tests only).
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")  # fsync on every commit
        self._conn.execute("PRAGMA busy_timeout=5000")  # wait, don't fail, on lock
        self._conn.executescript(_DDL)
        self._migrate_add_attempts()
        self._conn.commit()

    def _migrate_add_attempts(self) -> None:
        """Additive migration: add the ``attempts`` column to a pre-existing DB."""
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(wal_entries)")}
        if "attempts" not in cols:
            self._conn.execute(
                "ALTER TABLE wal_entries ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
            )

    # -----------------------------------------------------------------------
    # Write
    # -----------------------------------------------------------------------

    def enqueue(
        self,
        capsule_uri: str,
        capsule_sha256: str,
        tenant: str,
        run_id: str,
    ) -> int:
        """Enqueue a capsule for deferred NovaSeal signing.

        The write is fsync-durable before returning (PRAGMA synchronous=FULL).

        Returns:
            Row ID of the inserted WAL entry.
        """
        now_iso = datetime.now(tz=timezone.utc).isoformat()
        cur = self._conn.execute(
            """
            INSERT INTO wal_entries (capsule_uri, capsule_sha256, tenant, run_id, uploaded_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (capsule_uri, capsule_sha256, tenant, run_id, now_iso),
        )
        self._conn.commit()
        row_id = cur.lastrowid
        log.debug("WAL enqueue: row_id=%d capsule_uri=%s", row_id, capsule_uri)
        return row_id  # type: ignore[return-value]

    # -----------------------------------------------------------------------
    # Read / drain
    # -----------------------------------------------------------------------

    def pending_entries(self) -> list[tuple[int, WalEntry]]:
        """Return all undrained WAL entries as ``(row_id, WalEntry)`` tuples."""
        cur = self._conn.execute(
            """
            SELECT id, capsule_uri, capsule_sha256, tenant, run_id, uploaded_at
            FROM wal_entries
            WHERE drained = 0
            ORDER BY id
            """
        )
        result = []
        for row in cur.fetchall():
            row_id, capsule_uri, capsule_sha256, tenant, run_id, uploaded_at = row
            result.append((
                row_id,
                WalEntry(
                    capsule_uri=capsule_uri,
                    capsule_sha256=capsule_sha256,
                    tenant=tenant,
                    run_id=run_id,
                    uploaded_at=uploaded_at,
                ),
            ))
        return result

    def mark_drained(self, row_id: int) -> None:
        """Mark a WAL entry as successfully drained (signed + chain-committed)."""
        self._conn.execute(
            "UPDATE wal_entries SET drained = 1 WHERE id = ?",
            (row_id,),
        )
        self._conn.commit()
        log.debug("WAL drained: row_id=%d", row_id)

    def record_failure(self, row_id: int) -> int:
        """Increment the drain-attempt counter for a row; return the new count."""
        self._conn.execute(
            "UPDATE wal_entries SET attempts = attempts + 1 WHERE id = ?",
            (row_id,),
        )
        self._conn.commit()
        cur = self._conn.execute(
            "SELECT attempts FROM wal_entries WHERE id = ?", (row_id,)
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def mark_poisoned(self, row_id: int) -> None:
        """Dead-letter a row: exclude it from ``pending_entries`` permanently."""
        self._conn.execute(
            "UPDATE wal_entries SET drained = ? WHERE id = ?",
            (_DRAINED_POISONED, row_id),
        )
        self._conn.commit()
        log.warning("WAL poisoned (dead-lettered): row_id=%d", row_id)

    def poisoned_count(self) -> int:
        """Number of dead-lettered rows (for observability/alerting)."""
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM wal_entries WHERE drained = ?", (_DRAINED_POISONED,)
        )
        return int(cur.fetchone()[0])

    def pending_count(self) -> int:
        """Return the number of undrained entries."""
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM wal_entries WHERE drained = 0"
        )
        return cur.fetchone()[0]  # type: ignore[no-any-return]

    def total_count(self) -> int:
        """Return the total number of entries (drained + undrained)."""
        cur = self._conn.execute("SELECT COUNT(*) FROM wal_entries")
        return cur.fetchone()[0]  # type: ignore[no-any-return]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "LocalWal":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
