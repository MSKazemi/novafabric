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
from novafabric.server.pagination import (
    InvalidCursorError,
    ParsedCursor,
    encode_keyset_cursor,
    parse_cursor,
)

_DEFAULT_DB_PATH = Path.home() / ".novafabric" / "metadata.db"

_DEV_WARNING = (
    "[novafabric] WARNING: SQLiteMetadataStore is a dev-only, single-process backend. "
    "For production deployments use --backend postgres."
)

# ADR-0206 P2: total order for keyset pagination. SQLite already sorts NULL
# lowest (so last under DESC); the explicit ``NULLS LAST`` documents that and,
# unlike an ``started_at IS NULL`` sort expression, keeps the order usable by
# an index on ``(tenant_id, started_at DESC, run_id DESC)`` (see ADR-0206
# "Implementation status" — that index is a recorded follow-up, not shipped).
_RUNS_ORDER_BY = "started_at DESC NULLS LAST, run_id DESC"

# SQLite OFFSET is a signed 64-bit integer; larger legacy offsets are garbage.
_MAX_LEGACY_OFFSET = 2**63 - 1
# A legacy bare-integer cursor never needs more digits than the max offset.
_MAX_LEGACY_OFFSET_DIGITS = len(str(_MAX_LEGACY_OFFSET))


def _parse_store_cursor(cursor: str | None) -> ParsedCursor:
    """Parse a ``query_runs`` cursor, accepting the pre-P2 bare-integer form.

    The bare-integer offset string (``"50"``) is what this backend emitted
    before ADR-0206 P2; it is recognised first (a v1 cursor is base64 JSON
    and always starts with ``eyJ``, so the forms cannot collide). Everything
    else goes through the shared strict decoder in ``server.pagination`` —
    one cursor format, not a fork.

    Raises:
        InvalidCursorError: undecodable, unknown-version, malformed, negative
            or out-of-range cursor.
    """
    if cursor is not None and cursor.isascii() and cursor.isdigit():
        if len(cursor) > _MAX_LEGACY_OFFSET_DIGITS or int(cursor) > _MAX_LEGACY_OFFSET:
            raise InvalidCursorError("legacy offset cursor out of range")
        return ParsedCursor(kind="offset", offset=int(cursor))
    parsed = parse_cursor(cursor)
    if parsed.kind == "offset" and parsed.offset > _MAX_LEGACY_OFFSET:
        raise InvalidCursorError("legacy offset cursor out of range")
    return parsed


def _seek_predicate(key: tuple[str | None, str]) -> tuple[str, list[Any]]:
    """Return the SQL predicate selecting rows strictly after *key*.

    Under ``started_at DESC NULLS LAST, run_id DESC`` "after" means:

    * cursor in the non-NULL region ``(s, r)``: an older timestamp, or the
      same timestamp with a smaller ``run_id``, **or any NULL-``started_at``
      row** (the whole NULL tail sorts after every non-NULL value);
    * cursor in the NULL tail ``(None, r)``: a NULL-``started_at`` row with a
      smaller ``run_id`` only — never a non-NULL row, which all sort earlier.

    The comparisons are spelled out rather than using a row-value
    ``(started_at, run_id) < (?, ?)``, whose NULL semantics would silently
    drop the NULL tail.
    """
    started_at, run_id = key
    if started_at is None:
        return "(started_at IS NULL AND run_id < ?)", [run_id]
    return (
        "(started_at < ? OR (started_at = ? AND run_id < ?) OR started_at IS NULL)",
        [started_at, started_at, run_id],
    )


def _started_at_key(value: Any) -> str | None:
    """Normalise a stored ``started_at`` into the cursor's ``str | None`` slot."""
    if value is None or isinstance(value, str):
        return value
    return str(value)


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
        """Return ``(page, next_cursor)`` using keyset (seek) pagination.

        ADR-0206 P2 (experimental). Rows are ordered by
        ``started_at DESC NULLS LAST, run_id DESC`` — the ``run_id`` tiebreak
        makes the order total, so duplicate timestamps page deterministically.
        ``next_cursor`` is a v1 keyset cursor (``server.pagination`` format,
        ``{"v": 1, "k": [started_at, run_id]}``) naming the last row of the
        page; the next page seeks strictly past it, so it costs O(page) and
        rows inserted or deleted between pages cause neither duplicates nor
        skips of surviving rows.

        Legacy offset cursors — the bare-integer string this method emitted
        before P2 (e.g. ``"50"``) and the base64 ``{"offset": N}`` v0 form —
        are still honored for one deprecation cycle (ADR-0188): that page is
        served by ``LIMIT/OFFSET`` over the same order and its ``next_cursor``
        is a v1 keyset cursor, so an in-flight walk migrates after one page.

        Raises:
            InvalidCursorError: a non-empty cursor that is neither a valid v1
                keyset cursor nor a legacy offset cursor (tampered or garbage
                input fails loudly instead of restarting at page one).

        ``filters`` are accepted for interface compatibility and ignored, as
        before. No ``total`` is computed (the interface never returned one).
        """
        parsed = _parse_store_cursor(cursor)
        page_size = max(1, limit)
        sql = "SELECT * FROM runs WHERE tenant_id = ?"
        params: list[Any] = [str(tenant_id)]
        offset = 0
        if parsed.kind == "keyset" and parsed.key is not None:
            seek_sql, seek_params = _seek_predicate(parsed.key)
            sql += f" AND {seek_sql}"
            params.extend(seek_params)
        elif parsed.kind == "offset":
            offset = parsed.offset
        sql += f" ORDER BY {_RUNS_ORDER_BY} LIMIT ? OFFSET ?"
        params.extend([page_size + 1, offset])
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        has_more = len(rows) > page_size
        page = [dict(r) for r in rows[:page_size]]
        next_cursor: str | None = None
        if has_more:
            last = page[-1]
            next_cursor = encode_keyset_cursor(
                _started_at_key(last["started_at"]), str(last["run_id"])
            )
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
