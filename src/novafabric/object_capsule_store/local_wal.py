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
    drained       INTEGER NOT NULL DEFAULT 0,  -- 1 = drain complete
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_wal_undrained ON wal_entries (drained) WHERE drained = 0;
"""


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
        self._conn.executescript(_DDL)
        self._conn.commit()

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
