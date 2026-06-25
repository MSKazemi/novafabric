"""Basic integration tests for PostgresMetadataStore.

All tests in this module depend on the ``postgres_url`` session fixture
(defined in conftest.py).  If Docker is unavailable the entire module is
skipped gracefully.

TDD sequence:
  1. Run tests → ImportError / SKIP (before postgres.py exists)
  2. Implement postgres.py
  3. Run tests → all PASS (or SKIP if Docker unavailable)
"""
from __future__ import annotations

from uuid import uuid4

from novafabric.metadata_store.postgres import PostgresMetadataStore

# ---------------------------------------------------------------------------
# Test 1: bootstrap + health_check
# ---------------------------------------------------------------------------


def test_bootstrap_and_health(postgres_url: str) -> None:
    """bootstrap() creates schema; health_check returns status=ok, backend=postgres."""
    store = PostgresMetadataStore(dsn=postgres_url)
    store.bootstrap()

    health = store.health_check()
    assert health["status"] == "ok"
    assert health["backend"] == "postgres"


# ---------------------------------------------------------------------------
# Test 2: register_run + lookup_run round-trip inside begin_tenant_context
# ---------------------------------------------------------------------------


def test_register_and_lookup_run(postgres_url: str) -> None:
    """A run registered inside begin_tenant_context must be retrievable by (run_id, tenant_id)."""
    store = PostgresMetadataStore(dsn=postgres_url)
    store.bootstrap()

    run_id = uuid4()
    tenant_id = uuid4()

    with store.begin_tenant_context(tenant_id) as ctx:
        ctx.register_run(
            run_id,
            tenant_id,
            event_type="run.started",
            global_run_id=str(uuid4()),
            started_at="2026-05-13T00:00:00Z",
            world_size=1,
            expected_children=0,
        )
        result = ctx.lookup_run(run_id, tenant_id)

    assert result is not None
    assert str(result["run_id"]) == str(run_id)


# ---------------------------------------------------------------------------
# Test 3: query_runs pagination
# ---------------------------------------------------------------------------


def test_query_runs_pagination(postgres_url: str) -> None:
    """register 5 runs → query limit=3 returns 3 rows + cursor; cursor query → 2 rows + None."""
    store = PostgresMetadataStore(dsn=postgres_url)
    store.bootstrap()

    tenant_id = uuid4()

    with store.begin_tenant_context(tenant_id) as ctx:
        for _ in range(5):
            ctx.register_run(
                uuid4(),
                tenant_id,
                event_type="run.started",
                started_at="2026-05-13T00:00:00Z",
            )

        page1, cursor1 = ctx.query_runs(tenant_id, limit=3)
        assert len(page1) == 3
        assert cursor1 is not None

        page2, cursor2 = ctx.query_runs(tenant_id, limit=3, cursor=cursor1)
        assert len(page2) == 2
        assert cursor2 is None


# ---------------------------------------------------------------------------
# Test 4: register_run is idempotent
# ---------------------------------------------------------------------------


def test_register_run_is_idempotent(postgres_url: str) -> None:
    """Registering the same (run_id, tenant_id) twice must not raise and count stays at 1."""
    import psycopg

    store = PostgresMetadataStore(dsn=postgres_url)
    store.bootstrap()

    run_id = uuid4()
    tenant_id = uuid4()

    with store.begin_tenant_context(tenant_id) as ctx:
        ctx.register_run(run_id, tenant_id, event_type="run.started")
        ctx.register_run(run_id, tenant_id, event_type="run.started")  # must not raise

    # Verify count = 1 via a direct connection (no RLS needed for plain count)
    with psycopg.connect(postgres_url, row_factory=psycopg.rows.dict_row) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM runs WHERE run_id = %s AND tenant_id = %s",
            (str(run_id), str(tenant_id)),
        ).fetchone()
    assert row is not None
    assert row["cnt"] == 1
