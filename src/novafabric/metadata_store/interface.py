"""MetadataStore — abstract base class for the NovaFabric run/capsule indexing tier.

The metadata DB is a DERIVED INDEX over the object capsule store (ADR-0022).
It is NEVER the source of truth; it is rebuildable from raw capsules.

All multi-tenant write/read paths MUST open begin_tenant_context() first.
begin_tenant_context() issues SET LOCAL app.current_tenant_id inside an
open transaction — session-scoped SET is forbidden (FR-07, FR-13, ADR-001).
"""
from __future__ import annotations

import abc
from contextlib import AbstractContextManager
from typing import Any
from uuid import UUID


class RLSContextMissing(Exception):
    """Raised when begin_tenant_context() is called outside a transaction or on autocommit."""


class BackendModeError(Exception):
    """Raised when a backend is used in an unsupported configuration (e.g. SQLite multi-worker)."""


class MigrationVerificationFailed(Exception):
    """Raised when novafabric migrate-to-postgres checksum or row-count verification fails."""


class MetadataStore(abc.ABC):
    """Abstract indexing interface over NovaFabric run/capsule/signature metadata.

    Implementations:
      SQLiteMetadataStore  — dev-only, single-process; multi-worker raises BackendModeError.
      PostgresMetadataStore — production; psycopg[binary]>=3.2; SET LOCAL tenant isolation.

    All handler code MUST dispatch through this interface. Raw sqlite3 / psycopg imports
    are forbidden outside src/novafabric/metadata_store/ (CI grep gate, FR-13).
    """

    @abc.abstractmethod
    def register_run(self, run_id: UUID, tenant_id: UUID, **fields: Any) -> None:
        """Index a new run. Idempotent on (run_id, tenant_id) — ON CONFLICT DO NOTHING."""

    @abc.abstractmethod
    def lookup_run(self, run_id: UUID, tenant_id: UUID) -> dict[str, Any] | None:
        """Return run row dict or None if not found (within the tenant's RLS scope)."""

    @abc.abstractmethod
    def query_runs(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        cursor: str | None = None,
        **filters: Any,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return (page, next_cursor). Caller must be inside begin_tenant_context().

        ``cursor`` is opaque: pass back the previous ``next_cursor`` verbatim.
        ADR-0206 P2 (experimental): the SQLite backend emits v1 keyset cursors
        (``server.pagination`` format) over ``started_at DESC NULLS LAST,
        run_id DESC``; the Postgres backend still uses integer offsets (planned).
        """

    @abc.abstractmethod
    def begin_tenant_context(self, tenant_id: UUID) -> AbstractContextManager["MetadataStore"]:
        """Open a transaction and set the tenant scope.

        For PostgresMetadataStore: issues SET LOCAL app.current_tenant_id = $1.
        For SQLiteMetadataStore: no-op pass-through (single-tenant dev only).
        Raises RLSContextMissing if called on an autocommit connection.
        """

    @abc.abstractmethod
    def record_signature(
        self,
        run_id: UUID,
        tenant_id: UUID,
        signature_hash: str,
        payload: dict[str, Any],
    ) -> None:
        """Index a NovaSeal signature for a run. Idempotent on (run_id, signature_hash)."""

    def delete_run(self, run_id: UUID, tenant_id: UUID) -> int:
        """Remove one run's rows from the derived index; return runs rows removed (0 or 1).

        ADR-0206 P2 (experimental). **Index-only**: deletes the run's rows from the
        ``runs``, ``capsules`` and ``signatures`` tables for ``tenant_id`` in one
        transaction and never touches a capsule on disk (the index is derived and
        rebuildable, ADR-0022). Idempotent: an unindexed run returns 0.

        This is the *mechanism*, not the *policy*: it does not check legal holds,
        WORM locks or seals and writes no audit entry. Callers outside tests must go
        through :func:`novafabric.metadata_store.run_delete.delete_runs`, which does.

        Optional (non-abstract) so existing third-party stores keep working; a store
        that does not implement it raises :class:`NotImplementedError`. Caller must be
        inside ``begin_tenant_context()``.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement delete_run")

    @abc.abstractmethod
    def bootstrap(self) -> None:
        """Create schema and run migrations. Idempotent — safe to call on every startup."""

    @abc.abstractmethod
    def health_check(self) -> dict[str, Any]:
        """Return health dict with at least {'status': 'ok'|'degraded', 'backend': str}."""
