"""ADR-0221: opt-in psycopg connection pool for PostgresMetadataStore.

The security-critical test is `test_pool_preserves_tenant_isolation`: it drives
many tenant contexts through a small pool (forcing connection *reuse* across
tenants) and asserts that no tenant ever sees another tenant's rows — i.e. the
`SET LOCAL` GUC does not leak across pooled checkouts.

All tests depend on the `postgres_url` session fixture (conftest.py); the module
skips gracefully when Docker is unavailable.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("psycopg_pool")

from novafabric.metadata_store.postgres import PostgresMetadataStore  # noqa: E402


def test_with_pool_round_trip(postgres_url: str) -> None:
    """A pooled store registers and looks up a run like the unpooled one."""
    store = PostgresMetadataStore(dsn=postgres_url)
    store.bootstrap()

    pooled = PostgresMetadataStore.with_pool(postgres_url, min_size=1, max_size=2)
    try:
        run_id, tenant_id = uuid4(), uuid4()
        with pooled.begin_tenant_context(tenant_id) as ctx:
            ctx.register_run(run_id, tenant_id, event_type="run.started")
            result = ctx.lookup_run(run_id, tenant_id)
        assert result is not None
        assert str(result["run_id"]) == str(run_id)
    finally:
        pooled.close()


def test_pool_stats_reports_size(postgres_url: str) -> None:
    store = PostgresMetadataStore.with_pool(postgres_url, min_size=2, max_size=4)
    try:
        stats = store.pool_stats()
        assert stats is not None
        in_use, size = stats
        assert size >= 1
        assert in_use >= 0
    finally:
        store.close()
    # No pool → None (default construction path is unchanged).
    assert PostgresMetadataStore(dsn=postgres_url).pool_stats() is None


def test_pool_preserves_tenant_isolation(postgres_url: str) -> None:
    """SECURITY (ADR-0221 / gap-006): reused pooled connections must not leak the
    tenant GUC. Drive 12 distinct tenants through a max_size=2 pool — connections
    are reused across tenants — and assert each tenant sees only its own run."""
    bootstrap = PostgresMetadataStore(dsn=postgres_url)
    bootstrap.bootstrap()

    pooled = PostgresMetadataStore.with_pool(postgres_url, min_size=1, max_size=2)
    try:
        tenant_runs: list[tuple] = []
        for _ in range(12):
            tenant_id, run_id = uuid4(), uuid4()
            with pooled.begin_tenant_context(tenant_id) as ctx:
                ctx.register_run(run_id, tenant_id, event_type="run.started")
            tenant_runs.append((tenant_id, run_id))

        # Each tenant context sees exactly its own single run — never another's.
        for tenant_id, run_id in tenant_runs:
            with pooled.begin_tenant_context(tenant_id) as ctx:
                rows, _ = ctx.query_runs(tenant_id, limit=100)
            run_ids = {str(r["run_id"]) for r in rows}
            assert run_ids == {str(run_id)}, (
                f"tenant {tenant_id} saw {run_ids}, expected only {run_id} — "
                f"tenant GUC leaked across pooled connections"
            )
    finally:
        pooled.close()


def test_factory_returns_pooled_store_when_enabled(
    postgres_url: str, monkeypatch
) -> None:
    """NOVAFABRIC_METADATA_DB_POOL=1 makes the factory build a pooled store."""
    from novafabric.metadata_store.factory import get_metadata_store

    monkeypatch.setenv("NOVAFABRIC_METADATA_BACKEND", "postgres")
    monkeypatch.setenv("NOVAFABRIC_METADATA_DSN", postgres_url)

    # Off by default → no pool.
    monkeypatch.delenv("NOVAFABRIC_METADATA_DB_POOL", raising=False)
    plain = get_metadata_store()
    assert getattr(plain, "pool_stats", lambda: None)() is None

    # Opt-in → pooled store with a live pool.
    monkeypatch.setenv("NOVAFABRIC_METADATA_DB_POOL", "1")
    monkeypatch.setenv("NOVAFABRIC_METADATA_DB_POOL_MAX", "3")
    pooled = get_metadata_store()
    try:
        stats = pooled.pool_stats()  # type: ignore[attr-defined]
        assert stats is not None and stats[1] >= 1
    finally:
        pooled.close()  # type: ignore[attr-defined]


def test_pool_rejects_autocommit_connection(postgres_url: str, monkeypatch) -> None:
    """The FR-12 autocommit guard still fires on the pooled path."""
    pooled = PostgresMetadataStore.with_pool(postgres_url, min_size=1, max_size=1)
    try:
        import contextlib

        class _AutocommitConn:
            autocommit = True

            def close(self) -> None:  # pragma: no cover - not reached on pool path
                pass

        @contextlib.contextmanager
        def _fake_connection():
            yield _AutocommitConn()

        monkeypatch.setattr(pooled._pool, "connection", _fake_connection)

        from novafabric.metadata_store.interface import RLSContextMissing

        with pytest.raises(RLSContextMissing):
            with pooled.begin_tenant_context(uuid4()):
                pass
    finally:
        pooled.close()
