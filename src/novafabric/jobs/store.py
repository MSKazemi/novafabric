"""SQLite job store — lease-claimed, crash-recoverable (ADR-0242 D1/D2).

Correctness rules, each carried by the schema or a guarded statement rather
than a comment:

- **The claim is the lock.** ``claim()`` is one atomic
  ``UPDATE … RETURNING`` — never check-then-act (the xdist-race lesson:
  an "already done?" check is not a lock).
- **A lease, not ownership.** A worker that dies stops heartbeating; an
  expired ``running`` lease returns the job to ``queued`` (``attempt`` + 1)
  or to ``failed`` once ``max_attempts`` is exhausted — recovery is a
  property of the data model.
- **Terminal writes are worker-guarded.** ``finish``/``fail`` require the
  claiming worker id and ``state='running'``; a deposed worker's late result
  is a no-op, not a resurrection.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novafabric._paths import nova_home
from novafabric._sqlite_util import ensure_wal
from novafabric.jobs.models import Job, JobState


class JobStoreError(Exception):
    """Base class for job-store failures."""


class JobNotFoundError(JobStoreError):
    pass


class StaleWorkerError(JobStoreError):
    """A worker tried a terminal write on a job it no longer holds."""


def default_jobs_db_path() -> Path:
    """``$NOVAFABRIC_JOBS_DB`` or ``$NOVAFABRIC_HOME/jobs.db``."""
    env = os.environ.get("NOVAFABRIC_JOBS_DB")
    return Path(env) if env else nova_home() / "jobs.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id           TEXT PRIMARY KEY,
    kind             TEXT NOT NULL,
    tenant_id        TEXT NOT NULL DEFAULT 'default',
    payload          TEXT NOT NULL DEFAULT '{}',
    state            TEXT NOT NULL DEFAULT 'queued',
    attempt          INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 3,
    lease_expires_at REAL,
    worker_id        TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT,
    result           TEXT,
    error            TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim
    ON jobs (state, kind, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_tenant
    ON jobs (tenant_id, created_at);
"""


class JobStore:
    """Durable job bookkeeping over one SQLite file (WAL).

    Thread- and process-safe: every mutation is a single guarded statement,
    so concurrent workers (including separate ``--workers`` processes sharing
    the file) cannot double-claim or double-finish.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or default_jobs_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        ensure_wal(conn, what="jobs database")
        return conn

    # ---------- write side ----------

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        tenant_id: str = "default",
        max_attempts: int = 3,
        job_id: str | None = None,
    ) -> Job:
        if not kind.strip():
            raise ValueError("kind must not be empty")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        now = _now_iso()
        job_id = job_id or uuid.uuid4().hex
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO jobs (job_id, kind, tenant_id, payload, state,"
                " max_attempts, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)",
                (job_id, kind, tenant_id, json.dumps(payload or {}), max_attempts, now, now),
            )
        return self.get(job_id)

    def claim(
        self,
        worker_id: str,
        *,
        kinds: tuple[str, ...] | None = None,
        lease_seconds: float = 30.0,
    ) -> Job | None:
        """Atomically claim the oldest queued job (optionally by kind)."""
        now = _now_iso()
        kind_clause = ""
        params: list[Any] = []
        if kinds:
            kind_clause = f" AND kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "UPDATE jobs SET state='running', worker_id=?, attempt=attempt+1,"
                " lease_expires_at=?, started_at=COALESCE(started_at, ?), updated_at=?"
                " WHERE job_id = (SELECT job_id FROM jobs WHERE state='queued'"
                f"{kind_clause} ORDER BY created_at LIMIT 1)"
                " RETURNING *",
                [worker_id, time.time() + lease_seconds, now, now, *params],
            ).fetchone()
        return _row_to_job(row) if row else None

    def heartbeat(self, job_id: str, worker_id: str, *, lease_seconds: float = 30.0) -> bool:
        """Extend the lease; returns False if the worker no longer holds the job."""
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "UPDATE jobs SET lease_expires_at=?, updated_at=?"
                " WHERE job_id=? AND worker_id=? AND state='running'",
                (time.time() + lease_seconds, _now_iso(), job_id, worker_id),
            )
        return cur.rowcount == 1

    def finish(self, job_id: str, worker_id: str, result: dict[str, Any] | None = None) -> Job:
        return self._terminal(job_id, worker_id, JobState.SUCCEEDED, result=result)

    def fail(self, job_id: str, worker_id: str, error: str) -> Job:
        return self._terminal(job_id, worker_id, JobState.FAILED, error=error)

    def _terminal(
        self,
        job_id: str,
        worker_id: str,
        state: JobState,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> Job:
        now = _now_iso()
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "UPDATE jobs SET state=?, result=?, error=?, finished_at=?,"
                " updated_at=?, lease_expires_at=NULL"
                " WHERE job_id=? AND worker_id=? AND state='running'",
                (
                    state.value,
                    json.dumps(result) if result is not None else None,
                    error,
                    now,
                    now,
                    job_id,
                    worker_id,
                ),
            )
        if cur.rowcount != 1:
            # Either unknown, already terminal (e.g. cancelled meanwhile — the
            # late result is deliberately dropped), or claimed by a newer worker.
            job = self.get(job_id)
            raise StaleWorkerError(
                f"worker {worker_id!r} no longer holds job {job_id} "
                f"(state={job.state.value}, holder={job.worker_id!r})"
            )
        return self.get(job_id)

    def cancel(self, job_id: str) -> Job:
        """Cancel a job. Queued → cancelled; running → cancelled (cooperative:
        the executing thread is not signalled, and its late result is dropped
        by the ``finish``/``fail`` state guard)."""
        now = _now_iso()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE jobs SET state='cancelled', cancel_requested=1,"
                " finished_at=?, updated_at=?, lease_expires_at=NULL"
                " WHERE job_id=? AND state IN ('queued','running')",
                (now, now, job_id),
            )
        return self.get(job_id)

    def requeue_failed(self, job_id: str) -> Job:
        """Return a failed job to the queue for a bounded retry.

        The attempt counter is kept (``claim`` increments it), and the guard
        refuses once ``max_attempts`` is reached or cancellation was requested
        — retries are bounded by construction, not by caller discipline.
        """
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE jobs SET state='queued', worker_id=NULL, finished_at=NULL,"
                " updated_at=? WHERE job_id=? AND state='failed'"
                " AND attempt < max_attempts AND cancel_requested=0",
                (_now_iso(), job_id),
            )
        return self.get(job_id)

    def expire_leases(self) -> list[Job]:
        """Recover jobs whose worker died: expired ``running`` leases return to
        ``queued`` while attempts remain, else become ``failed`` — visibly,
        never silently. Returns the affected jobs."""
        now_epoch = time.time()
        now = _now_iso()
        with closing(self._connect()) as conn, conn:
            requeued = conn.execute(
                "UPDATE jobs SET state='queued', worker_id=NULL,"
                " lease_expires_at=NULL, updated_at=?"
                " WHERE state='running' AND lease_expires_at < ?"
                " AND attempt < max_attempts RETURNING *",
                (now, now_epoch),
            ).fetchall()
            exhausted = conn.execute(
                "UPDATE jobs SET state='failed', worker_id=NULL,"
                " lease_expires_at=NULL, finished_at=?, updated_at=?,"
                " error=COALESCE(error, 'lease expired after ' || attempt ||"
                " ' attempt(s) — worker died or process restarted')"
                " WHERE state='running' AND lease_expires_at < ?"
                " AND attempt >= max_attempts RETURNING *",
                (now, now, now_epoch),
            ).fetchall()
        return [_row_to_job(r) for r in (*requeued, *exhausted)]

    # ---------- read side ----------

    def get(self, job_id: str) -> Job:
        with closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFoundError(f"job {job_id} not found")
        return _row_to_job(row)

    def list_jobs(
        self,
        *,
        tenant_id: str | None = None,
        state: JobState | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[Job]:
        clauses, params = [], []
        for col, val in (("tenant_id", tenant_id), ("kind", kind)):
            if val is not None:
                clauses.append(f"{col}=?")
                params.append(val)
        if state is not None:
            clauses.append("state=?")
            params.append(state.value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                f"SELECT * FROM jobs{where} ORDER BY created_at DESC LIMIT ?",
                (*params, max(1, min(limit, 1000))),
            ).fetchall()
        return [_row_to_job(r) for r in rows]


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        job_id=row["job_id"],
        kind=row["kind"],
        tenant_id=row["tenant_id"],
        payload=json.loads(row["payload"]),
        state=JobState(row["state"]),
        attempt=row["attempt"],
        max_attempts=row["max_attempts"],
        lease_expires_at=row["lease_expires_at"],
        worker_id=row["worker_id"],
        cancel_requested=bool(row["cancel_requested"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        result=json.loads(row["result"]) if row["result"] else None,
        error=row["error"],
    )
