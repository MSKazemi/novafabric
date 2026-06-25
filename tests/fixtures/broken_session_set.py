"""BrokenMetadataStore — uses session-scoped SET (not SET LOCAL). FOR TESTING ONLY.

This is the mutant baseline for FR-08 tc-005. It MUST fail the cross-tenant
isolation test. Do NOT use in production.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from uuid import UUID

import psycopg
import psycopg.rows

from novafabric.metadata_store.interface import MetadataStore


class BrokenMetadataStore(MetadataStore):
    """Intentionally broken — uses session-scoped SET. FOR TESTING ONLY."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._active_conn: psycopg.Connection[Any] | None = None

    def _connect(self) -> psycopg.Connection[Any]:
        return psycopg.connect(self._dsn, row_factory=psycopg.rows.dict_row)

    def bootstrap(self) -> None:
        pass  # Schema already created by PostgresMetadataStore.bootstrap()

    @contextmanager  # type: ignore[override]
    def begin_tenant_context(self, tenant_id: UUID):  # type: ignore[override]
        conn = self._connect()
        with conn.transaction():
            # BUG: session-scoped SET — leaks across pgBouncer transaction-mode connections
            conn.execute(f"SET app.current_tenant_id = '{tenant_id}'")  # noqa: S608
            self._active_conn = conn
            try:
                yield self
            finally:
                self._active_conn = None
        conn.close()

    def register_run(self, run_id: UUID, tenant_id: UUID, **fields: Any) -> None:
        assert self._active_conn is not None
        self._active_conn.execute(
            "INSERT INTO runs (run_id, tenant_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (str(run_id), str(tenant_id)),
        )

    def lookup_run(self, run_id: UUID, tenant_id: UUID) -> dict[str, Any] | None:
        assert self._active_conn is not None
        return self._active_conn.execute(
            "SELECT * FROM runs WHERE run_id=%s AND tenant_id=%s",
            (str(run_id), str(tenant_id)),
        ).fetchone()

    def query_runs(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        cursor: str | None = None,
        **filters: Any,
    ) -> tuple[list[dict[str, Any]], str | None]:
        assert self._active_conn is not None
        offset = int(cursor) if cursor else 0
        rows = self._active_conn.execute(
            "SELECT * FROM runs WHERE tenant_id=%s LIMIT %s OFFSET %s",
            (str(tenant_id), limit, offset),
        ).fetchall()
        return [dict(r) for r in rows], None

    def record_signature(
        self,
        run_id: UUID,
        tenant_id: UUID,
        signature_hash: str,
        payload: dict[str, Any],
    ) -> None:
        pass  # Not exercised in isolation test

    def health_check(self) -> dict[str, Any]:
        return {"status": "ok", "backend": "broken-session-set"}
