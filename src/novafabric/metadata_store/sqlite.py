"""SQLiteMetadataStore — dev-only, single-process MetadataStore implementation.

This backend is intentionally limited:
- Single-process only (no multi-worker support).
- No RLS — single-tenant isolation is enforced by the caller.
- Schema is WAL-journal, synchronous=NORMAL for local-mode speed.
- Do NOT use in production. Use PostgresMetadataStore instead.

FR-02: dev-only SQLite backend (ADR-0040).
"""
from __future__ import annotations

import json
import os
import sqlite3
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator
from uuid import UUID

from novafabric.metadata_store.interface import BackendModeError, MetadataStore

_DEFAULT_DB_PATH = Path.home() / ".novafabric" / "metadata.db"

_DEV_WARNING = (
    "[novafabric] WARNING: SQLiteMetadataStore is a dev-only, single-process backend. "
    "For production deployments use --backend postgres."
)


class SQLiteMetadataStore(MetadataStore):
    """Dev-only, single-process SQLite implementation of MetadataStore.

    Construction raises ``BackendModeError`` immediately if
    ``NOVAFABRIC_API_WORKERS`` is set to a value > 1.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.  Defaults to
        ``~/.novafabric/metadata.db``.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        workers_raw = os.environ.get("NOVAFABRIC_API_WORKERS", "1")
        try:
            workers = int(workers_raw)
        except ValueError:
            workers = 1

        if workers > 1:
            raise BackendModeError(
                f"SQLite backend does not support multi-worker mode; "
                f"NOVAFABRIC_API_WORKERS={workers}. "
                f"Use --backend postgres."
            )

        warnings.warn(_DEV_WARNING, UserWarning, stacklevel=2)

        self._db_path = Path(db_path) if db_path is not None else _DEFAULT_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ------------------------------------------------------------------
    # MetadataStore abstract methods
    # ------------------------------------------------------------------

    def bootstrap(self) -> None:
        """Create schema tables idempotently. Safe to call on every startup."""
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id            TEXT,
                    tenant_id         TEXT,
                    event_type        TEXT,
                    global_run_id     TEXT,
                    started_at        TEXT,
                    status            TEXT DEFAULT 'pending',
                    world_size        INTEGER,
                    expected_children INTEGER,
                    children_arrived  INTEGER DEFAULT 0,
                    PRIMARY KEY (run_id, tenant_id)
                );

                CREATE TABLE IF NOT EXISTS capsules (
                    capsule_uri  TEXT PRIMARY KEY,
                    run_id       TEXT,
                    tenant_id    TEXT
                );

                CREATE TABLE IF NOT EXISTS signatures (
                    run_id         TEXT,
                    tenant_id      TEXT,
                    signature_hash TEXT,
                    payload_json   TEXT,
                    PRIMARY KEY (run_id, signature_hash)
                );

                CREATE TABLE IF NOT EXISTS retention_policies (
                    tenant_id   TEXT PRIMARY KEY,
                    policy_json TEXT
                );
                """
            )
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        """Create performance indexes for hot query paths.

        Safe to call multiple times — all statements use ``IF NOT EXISTS``.
        Covers the most frequent dashboard query patterns:
        - Runs ordered by recency (started_at DESC)
        - Runs filtered by status or global_run_id
        - Capsules looked up by run
        """
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_runs_started_at
                    ON runs(started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_runs_status
                    ON runs(status);
                CREATE INDEX IF NOT EXISTS idx_runs_global_run_id
                    ON runs(global_run_id);
                CREATE INDEX IF NOT EXISTS idx_runs_tenant_status
                    ON runs(tenant_id, status);
                CREATE INDEX IF NOT EXISTS idx_capsules_run_id
                    ON capsules(run_id);
                CREATE INDEX IF NOT EXISTS idx_capsules_tenant_id
                    ON capsules(tenant_id);
                """
            )

    def register_run(self, run_id: UUID, tenant_id: UUID, **fields: Any) -> None:
        """Index a new run.  Idempotent — silently ignores duplicate (run_id, tenant_id)."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO runs
                    (run_id, tenant_id, event_type, global_run_id, started_at,
                     world_size, expected_children)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(run_id),
                    str(tenant_id),
                    fields.get("event_type"),
                    fields.get("global_run_id"),
                    fields.get("started_at"),
                    fields.get("world_size"),
                    fields.get("expected_children"),
                ),
            )

    def lookup_run(self, run_id: UUID, tenant_id: UUID) -> dict[str, Any] | None:
        """Return run row dict or None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ? AND tenant_id = ?",
                (str(run_id), str(tenant_id)),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def query_runs(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        cursor: str | None = None,
        **filters: Any,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return (page, next_cursor) with integer-offset cursor pagination."""
        offset = int(cursor) if cursor is not None else 0
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE tenant_id = ? LIMIT ? OFFSET ?",
                (str(tenant_id), limit + 1, offset),
            ).fetchall()

        has_more = len(rows) > limit
        page = [dict(r) for r in rows[:limit]]
        next_cursor: str | None = str(offset + limit) if has_more else None
        return page, next_cursor

    @contextmanager
    def begin_tenant_context(self, tenant_id: UUID) -> Generator["SQLiteMetadataStore", None, None]:
        """No-op pass-through — SQLite is single-tenant dev only.

        Yields ``self`` so callers can write::

            with store.begin_tenant_context(tenant_id) as ctx:
                ctx.register_run(...)
        """
        yield self

    def record_signature(
        self,
        run_id: UUID,
        tenant_id: UUID,
        signature_hash: str,
        payload: dict[str, Any],
    ) -> None:
        """Index a NovaSeal signature.  Idempotent on (run_id, signature_hash)."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO signatures
                    (run_id, tenant_id, signature_hash, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    str(run_id),
                    str(tenant_id),
                    signature_hash,
                    json.dumps(payload),
                ),
            )

    def health_check(self) -> dict[str, Any]:
        """Return health dict with status and backend info."""
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1").fetchone()
            return {
                "status": "ok",
                "backend": "sqlite",
                "db_path": str(self._db_path),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "degraded",
                "backend": "sqlite",
                "db_path": str(self._db_path),
                "error": str(exc),
            }
