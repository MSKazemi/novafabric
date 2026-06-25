"""Tests for the SET LOCAL tenant-isolation invariant (FR-07, FR-12).

These tests verify that:
  1. begin_tenant_context() rejects autocommit connections (gap-006 cross-tenant
     leak vector) and raises RLSContextMissing with "autocommit" in the message.
  2. All DML methods raise RLSContextMissing when called outside begin_tenant_context().

Both tests skip gracefully if the ``postgres_url`` fixture skips (Docker unavailable).
"""
from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest

from novafabric.metadata_store import RLSContextMissing
from novafabric.metadata_store.postgres import PostgresMetadataStore

# ---------------------------------------------------------------------------
# Test 1: autocommit connection must be rejected
# ---------------------------------------------------------------------------


def test_begin_tenant_context_requires_transaction(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """begin_tenant_context() must raise RLSContextMissing for autocommit connections.

    Session-scoped SET (without LOCAL) would silently leak the tenant GUC across
    connection reuse (pgBouncer session pooling gap-006).  We guard against this by
    rejecting autocommit connections entirely.
    """
    store = PostgresMetadataStore(dsn=postgres_url)
    store.bootstrap()

    # Open a genuine autocommit connection to inject
    autocommit_conn = psycopg.connect(postgres_url, autocommit=True)

    def _fake_connect(*args, **kwargs):  # noqa: ANN002, ANN003
        return autocommit_conn

    monkeypatch.setattr(store, "_connect", _fake_connect)

    try:
        with pytest.raises(RLSContextMissing) as exc_info:
            with store.begin_tenant_context(uuid4()):
                pass  # should never reach here

        assert "autocommit" in str(exc_info.value).lower()
    finally:
        # Close the injected connection if it wasn't already closed by the guard
        if not autocommit_conn.closed:
            autocommit_conn.close()


# ---------------------------------------------------------------------------
# Test 2: DML without context raises RLSContextMissing
# ---------------------------------------------------------------------------


def test_operations_outside_context_raise(postgres_url: str) -> None:
    """register_run() called outside begin_tenant_context() must raise RLSContextMissing."""
    store = PostgresMetadataStore(dsn=postgres_url)
    store.bootstrap()

    with pytest.raises(RLSContextMissing):
        store.register_run(run_id=uuid4(), tenant_id=uuid4())
