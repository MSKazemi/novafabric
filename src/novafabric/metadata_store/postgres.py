"""PostgresMetadataStore — production MetadataStore implementation.

Uses psycopg3 (psycopg>=3.2) with per-transaction SET LOCAL tenant GUC isolation.

Security invariant (FR-07, FR-12, gap-006):
  ``begin_tenant_context()`` MUST use ``SET LOCAL app.current_tenant_id`` to scope
  the tenant GUC to the current transaction.  A session-scoped GUC assignment
  (i.e. without LOCAL) would leak the tenant context across connection reuse in
  pgBouncer session pooling — the gap-006 cross-tenant leak vector.

  A CI grep gate (FR-07) verifies that no bare session-scoped GUC assignment
  appears in this file.

  Citation resolved 2026-05-27 (ADR-0050/ADR-0052): SET LOCAL is the documented
  pgBouncer + RLS pattern per PostgreSQL docs §SET LOCAL
  (https://www.postgresql.org/docs/current/sql-set.html), the pganalyze RLS guide
  (https://pganalyze.com/blog/postgres-row-level-security-ruby-rails), and the
  Supabase Row Level Security docs
  (https://supabase.com/docs/guides/database/postgres/row-level-security).
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Generator
from uuid import UUID

import psycopg
import psycopg.rows

from novafabric.metadata_store.interface import MetadataStore, RLSContextMissing

# Module-level ContextVar so each asyncio task / thread gets its own active connection.
# Using an instance attribute would break under concurrent request handling.
_active_conn_var: ContextVar[psycopg.Connection[Any] | None] = ContextVar(
    "_active_conn", default=None
)

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id            UUID,
    tenant_id         UUID,
    event_type        TEXT,
    global_run_id     UUID,
    started_at        TIMESTAMPTZ,
    status            TEXT DEFAULT 'pending',
    world_size        BIGINT,
    expected_children BIGINT,
    children_arrived  BIGINT DEFAULT 0,
    PRIMARY KEY (run_id, tenant_id)
);

CREATE TABLE IF NOT EXISTS capsules (
    capsule_uri  TEXT PRIMARY KEY,
    run_id       UUID,
    tenant_id    UUID
);

CREATE TABLE IF NOT EXISTS signatures (
    run_id         UUID,
    tenant_id      UUID,
    signature_hash TEXT,
    payload_json   JSONB,
    PRIMARY KEY (run_id, signature_hash)
);

CREATE TABLE IF NOT EXISTS retention_policies (
    tenant_id   UUID PRIMARY KEY,
    policy_json JSONB
);
"""

# Tenant-scoped tables that require FORCE ROW LEVEL SECURITY + tenant_isolation policy.
_TENANT_TABLES = ("runs", "capsules", "signatures", "retention_policies")

# RLS roles + policy DDL applied by bootstrap() for local-mode and test environments.
# In production, Alembic v001_init.py applies these in a managed migration.
_RLS_ROLE_DDL = """
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'novafabric_app') THEN
    CREATE ROLE novafabric_app NOLOGIN NOBYPASSRLS;
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'novafabric_migrator') THEN
    CREATE ROLE novafabric_migrator NOLOGIN BYPASSRLS;
  END IF;
END $$;
"""


class PostgresMetadataStore(MetadataStore):
    """Production Postgres implementation of MetadataStore.

    Parameters
    ----------
    dsn:
        libpq-compatible connection string, e.g.
        ``postgresql://user:password@host:5432/dbname``.

    Thread-safety
    -------------
    The active connection is tracked in a ``contextvars.ContextVar`` so that each
    asyncio task / OS thread gets its own context.  Never pass a store instance
    across threads while a tenant context is open.
    """

    def __init__(self, dsn: str, *, pool: Any = None) -> None:
        self._dsn = dsn
        # Optional psycopg_pool.ConnectionPool (ADR-0221). When set,
        # begin_tenant_context checks out from the pool instead of opening a
        # fresh connection per request. Default None → unchanged connect/close.
        self._pool = pool

    @classmethod
    def with_pool(
        cls,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
    ) -> "PostgresMetadataStore":
        """Construct a store backed by an opt-in psycopg connection pool (ADR-0221).

        The pool opens connections with ``autocommit=False`` (the FR-12
        invariant) and the dict row factory. RLS tenant isolation is preserved
        because ``SET LOCAL`` is transaction-scoped: it is cleared when
        ``begin_tenant_context``'s transaction ends, before the connection is
        returned to the pool; the pool's default reset rolls back any residual
        transaction as defense-in-depth.
        """
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(
            conninfo=dsn,
            min_size=min_size,
            max_size=max_size,
            kwargs={"autocommit": False, "row_factory": psycopg.rows.dict_row},
            open=True,
        )
        return cls(dsn, pool=pool)

    def pool_stats(self) -> tuple[int, int] | None:
        """Return ``(in_use, size)`` for the pool, or None when no pool is set.

        Intended for sampling into the ``nova_db_pool_*`` gauges
        (``server/observability.py::set_db_pool``).
        """
        if self._pool is None:
            return None
        stats = self._pool.get_stats()
        size = int(stats.get("pool_size", 0))
        available = int(stats.get("pool_available", 0))
        return (max(size - available, 0), size)

    def close(self) -> None:
        """Close the connection pool, if one is configured."""
        if self._pool is not None:
            self._pool.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> psycopg.Connection[Any]:
        """Open a new psycopg3 connection with dict row factory."""
        return psycopg.connect(self._dsn, row_factory=psycopg.rows.dict_row)

    def _conn(self) -> psycopg.Connection[Any]:
        """Return the active tenant-context connection or raise RLSContextMissing."""
        conn = _active_conn_var.get()
        if conn is None:
            raise RLSContextMissing(
                "No active tenant context. "
                "All DML operations must be called inside begin_tenant_context()."
            )
        return conn

    # ------------------------------------------------------------------
    # MetadataStore abstract methods
    # ------------------------------------------------------------------

    def bootstrap(self) -> None:
        """Create schema tables + RLS policies idempotently.  Safe to call on every startup.

        Applies:
        - Table DDL (IF NOT EXISTS)
        - novafabric_app / novafabric_migrator roles (IF NOT EXISTS)
        - ENABLE + FORCE ROW LEVEL SECURITY on all tenant-scoped tables
        - ``tenant_isolation`` policy (DROP IF EXISTS → CREATE for idempotency)

        In production, these are also applied by Alembic v001_init.  Calling
        bootstrap() is safe even if the migration has already run.
        """
        with self._connect() as conn:
            conn.execute(_DDL_SQL)
            # Roles
            conn.execute(_RLS_ROLE_DDL)
            # Per-table RLS
            for table in _TENANT_TABLES:
                conn.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")  # noqa: S608
                conn.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")  # noqa: S608
                conn.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")  # noqa: S608
                conn.execute(
                    f"""
                    CREATE POLICY tenant_isolation ON {table}
                        USING (
                            tenant_id = current_setting('app.current_tenant_id')::uuid
                        )
                        WITH CHECK (
                            tenant_id = current_setting('app.current_tenant_id')::uuid
                        )
                    """  # noqa: S608
                )
            conn.commit()

    @contextmanager
    def begin_tenant_context(
        self, tenant_id: UUID
    ) -> Generator["PostgresMetadataStore", None, None]:
        """Open a transaction and issue SET LOCAL app.current_tenant_id.

        Raises
        ------
        RLSContextMissing
            If the connection is in autocommit mode (FR-12).  Autocommit connections
            cannot use SET LOCAL because there is no transaction scope to bind to;
            a session-scoped SET would create the gap-006 cross-tenant GUC leak.
        """
        # Acquire from the pool (ADR-0221) when configured, else open a fresh
        # connection. The pooled connection is returned to the pool on exit; the
        # unpooled one is closed. Both go through the identical RLS body below.
        if self._pool is not None:
            pool_cm = self._pool.connection()
            conn = pool_cm.__enter__()
        else:
            pool_cm = None
            conn = self._connect()

        # FR-12 guard: autocommit connections are forbidden.
        # psycopg3 starts with autocommit=False by default, but a caller might
        # inject one (e.g. via monkeypatch in tests) or a future refactor could
        # introduce one inadvertently.  Reject early and loudly.
        if conn.autocommit:
            if pool_cm is not None:
                pool_cm.__exit__(None, None, None)
            else:
                conn.close()
            raise RLSContextMissing(
                "begin_tenant_context requires an open transaction; "
                "autocommit connections are forbidden (FR-12)."
            )

        token = _active_conn_var.set(conn)
        try:
            with conn.transaction():
                # SECURITY: SET LOCAL scopes the GUC to the current transaction.
                # Never use bare SET here — see module docstring. Because it is
                # transaction-scoped, the tenant GUC is cleared here before a
                # pooled connection is returned to the pool (ADR-0221).
                #
                # Postgres SET commands do not accept parameterised values ($1 / %s);
                # the value must be a literal string.  We sanitise tenant_id by
                # UUID-casting it first so there is no injection risk — a UUID can
                # only contain hex digits and hyphens.
                safe_tid = str(UUID(str(tenant_id)))  # re-parse to guarantee UUID format
                conn.execute(f"SET LOCAL app.current_tenant_id = '{safe_tid}'")  # noqa: S608
                yield self
        finally:
            _active_conn_var.reset(token)
            if pool_cm is not None:
                pool_cm.__exit__(None, None, None)
            else:
                conn.close()

    def register_run(self, run_id: UUID, tenant_id: UUID, **fields: Any) -> None:
        """Index a new run.  Idempotent on (run_id, tenant_id) — ON CONFLICT DO NOTHING."""
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO runs
                (run_id, tenant_id, event_type, global_run_id, started_at,
                 world_size, expected_children)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, tenant_id) DO NOTHING
            """,
            (
                str(run_id),
                str(tenant_id),
                fields.get("event_type"),
                str(fields["global_run_id"]) if fields.get("global_run_id") else None,
                fields.get("started_at"),
                fields.get("world_size"),
                fields.get("expected_children"),
            ),
        )

    def lookup_run(self, run_id: UUID, tenant_id: UUID) -> dict[str, Any] | None:
        """Return run row dict or None if not found within the tenant's scope."""
        conn = self._conn()
        row: dict[str, Any] | None = conn.execute(
            "SELECT * FROM runs WHERE run_id = %s AND tenant_id = %s",
            (str(run_id), str(tenant_id)),
        ).fetchone()
        return row

    def query_runs(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        cursor: str | None = None,
        **filters: Any,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return (page, next_cursor) with integer-offset cursor pagination.

        Rows are ordered by ``started_at DESC NULLS LAST``.
        """
        conn = self._conn()
        offset = int(cursor) if cursor is not None else 0
        rows: list[dict[str, Any]] = conn.execute(
            """
            SELECT * FROM runs
            WHERE tenant_id = %s
            ORDER BY started_at DESC NULLS LAST
            LIMIT %s OFFSET %s
            """,
            (str(tenant_id), limit + 1, offset),
        ).fetchall()

        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor: str | None = str(offset + limit) if has_more else None
        return page, next_cursor

    def record_signature(
        self,
        run_id: UUID,
        tenant_id: UUID,
        signature_hash: str,
        payload: dict[str, Any],
    ) -> None:
        """Index a NovaSeal signature.  Idempotent on (run_id, signature_hash)."""
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO signatures (run_id, tenant_id, signature_hash, payload_json)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (run_id, signature_hash) DO NOTHING
            """,
            (
                str(run_id),
                str(tenant_id),
                signature_hash,
                json.dumps(payload),
            ),
        )

    def health_check(self) -> dict[str, Any]:
        """Return health dict with status=ok|degraded and backend=postgres."""
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1")
            return {"status": "ok", "backend": "postgres"}
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "degraded",
                "backend": "postgres",
                "error": str(exc),
            }
