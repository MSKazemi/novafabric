"""Writer lease with fencing tokens (ADR-0244 D1/D2, slice 1).

One single-row table anchored in the same store the writer must reach anyway
(Postgres in server mode; the SQLite twin keeps local-mode semantics testable
and the two implementations behaviorally identical). Correctness properties:

- **Acquisition is one atomic upsert** — never check-then-act. Takeover is
  possible only from an *expired* lease (or re-acquisition by the current
  holder), and every change of holder increments the **fencing token**, a
  monotonic counter that write paths will verify in later slices: a deposed
  writer holds a lower token than the current lease and its guarded writes
  are rejected at the data layer.
- **Renewal is holder-guarded**: a renew by anything but the current,
  unexpired holder returns ``False`` — a writer that lost its lease learns
  so on its next heartbeat and must halt mutating work (ADR-0244 D4).
- **Time comes from the caller** (``time.time()``): the lease margin, not
  clock perfection, carries safety — document NTP as a deployment
  requirement; the fencing check (not the clock) is the split-brain
  backstop.
"""

from __future__ import annotations

import sqlite3
import time
from abc import ABC, abstractmethod
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from novafabric._sqlite_util import ensure_wal


@dataclass(frozen=True)
class LeaseState:
    holder_id: str | None
    fencing_token: int
    expires_at: float

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.expires_at


class WriterLeaseStore(ABC):
    """The single-writer lease contract (ADR-0244 D1)."""

    @abstractmethod
    def acquire(self, holder_id: str, ttl_seconds: float) -> LeaseState | None:
        """Try to take (or re-take) the lease.

        Returns the resulting :class:`LeaseState` when ``holder_id`` now
        holds it, else ``None`` (someone else holds an unexpired lease).
        A change of holder increments the fencing token.
        """

    @abstractmethod
    def renew(self, holder_id: str, ttl_seconds: float) -> bool:
        """Extend the lease iff ``holder_id`` is the current, unexpired holder."""

    @abstractmethod
    def release(self, holder_id: str) -> bool:
        """Voluntarily give up the lease (clean demotion). Holder-guarded."""

    @abstractmethod
    def current(self) -> LeaseState | None:
        """The current lease row, or ``None`` before first acquisition."""


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS writer_lease (
    singleton     INTEGER PRIMARY KEY CHECK (singleton = 1),
    holder_id     TEXT,
    fencing_token INTEGER NOT NULL DEFAULT 0,
    expires_at    REAL NOT NULL DEFAULT 0
);
"""

# One atomic statement for acquire, shared shape across both dialects:
# take the row when it is expired, unheld, or already ours; bump the fencing
# token exactly when the holder changes.
_ACQUIRE_SQL = """
UPDATE writer_lease SET
    fencing_token = fencing_token + (CASE WHEN holder_id IS :h THEN 0 ELSE 1 END),
    holder_id = :h,
    expires_at = :exp
WHERE singleton = 1 AND (holder_id IS NULL OR holder_id = :h OR expires_at <= :now)
RETURNING holder_id, fencing_token, expires_at
"""

_RENEW_SQL = """
UPDATE writer_lease SET expires_at = :exp
WHERE singleton = 1 AND holder_id = :h AND expires_at > :now
"""

_RELEASE_SQL = """
UPDATE writer_lease SET holder_id = NULL, expires_at = 0
WHERE singleton = 1 AND holder_id = :h
"""


class SqliteLeaseStore(WriterLeaseStore):
    """Local/SQLite twin — same semantics, unit-test speed."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript(_SQLITE_SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO writer_lease (singleton) VALUES (1)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        ensure_wal(conn, what="lease database")
        return conn

    def acquire(self, holder_id: str, ttl_seconds: float) -> LeaseState | None:
        now = time.time()
        params = {"h": holder_id, "exp": now + ttl_seconds, "now": now}
        with closing(self._connect()) as conn, conn:
            row = conn.execute(_ACQUIRE_SQL, params).fetchone()
        if row is None:
            return None
        return LeaseState(row["holder_id"], row["fencing_token"], row["expires_at"])

    def renew(self, holder_id: str, ttl_seconds: float) -> bool:
        now = time.time()
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                _RENEW_SQL, {"h": holder_id, "exp": now + ttl_seconds, "now": now}
            )
        return cur.rowcount == 1

    def release(self, holder_id: str) -> bool:
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(_RELEASE_SQL, {"h": holder_id})
        return cur.rowcount == 1

    def current(self) -> LeaseState | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT holder_id, fencing_token, expires_at FROM writer_lease"
                " WHERE singleton = 1"
            ).fetchone()
        if row is None or (row["holder_id"] is None and row["fencing_token"] == 0):
            return None
        return LeaseState(row["holder_id"], row["fencing_token"], row["expires_at"])


class PostgresLeaseStore(WriterLeaseStore):
    """Postgres lease — the server-mode anchor (ADR-0244 D1).

    Uses the same single-statement semantics as the SQLite twin; parity is
    asserted by the shared test contract in ``tests/ha/`` plus the
    testcontainers tier.
    """

    _PG_SCHEMA = """
    CREATE TABLE IF NOT EXISTS writer_lease (
        singleton     INTEGER PRIMARY KEY CHECK (singleton = 1),
        holder_id     TEXT,
        fencing_token BIGINT NOT NULL DEFAULT 0,
        expires_at    DOUBLE PRECISION NOT NULL DEFAULT 0
    );
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        with self._conn() as conn, conn.cursor() as cur:
            # Concurrent CREATE TABLE IF NOT EXISTS races in Postgres
            # (pg_type unique violation); serialize bootstrap with a
            # transaction-scoped advisory lock.
            cur.execute("SELECT pg_advisory_xact_lock(752440244)")
            cur.execute(self._PG_SCHEMA)
            cur.execute(
                "INSERT INTO writer_lease (singleton) VALUES (1)"
                " ON CONFLICT (singleton) DO NOTHING"
            )

    def _conn(self) -> Any:
        import psycopg

        return psycopg.connect(self._dsn)

    @staticmethod
    def _pg(sql: str) -> str:
        return (
            sql.replace(":h", "%(h)s").replace(":exp", "%(exp)s").replace(":now", "%(now)s")
        )

    def acquire(self, holder_id: str, ttl_seconds: float) -> LeaseState | None:
        now = time.time()
        params = {"h": holder_id, "exp": now + ttl_seconds, "now": now}
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                self._pg(_ACQUIRE_SQL).replace("IS %(h)s", "IS NOT DISTINCT FROM %(h)s"),
                params,
            )
            row = cur.fetchone()
        if row is None:
            return None
        return LeaseState(row[0], row[1], row[2])

    def renew(self, holder_id: str, ttl_seconds: float) -> bool:
        now = time.time()
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                self._pg(_RENEW_SQL), {"h": holder_id, "exp": now + ttl_seconds, "now": now}
            )
            return bool(cur.rowcount == 1)

    def release(self, holder_id: str) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(self._pg(_RELEASE_SQL), {"h": holder_id})
            return bool(cur.rowcount == 1)

    def current(self) -> LeaseState | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT holder_id, fencing_token, expires_at FROM writer_lease"
                " WHERE singleton = 1"
            )
            row = cur.fetchone()
        if row is None or (row[0] is None and row[1] == 0):
            return None
        return LeaseState(row[0], row[1], row[2])
