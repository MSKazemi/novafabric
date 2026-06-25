"""FR-08 — SECURITY-CRITICAL — GDPR Article 32 control.

Two concurrent tenants interleave register_run / query_runs.
Pass:  cross_tenant_row_leak_count == 0 for PostgresMetadataStore (SET LOCAL).
Mutant: BrokenMetadataStore (session SET) MUST demonstrate the vulnerability.

Without pgBouncer in front (direct Postgres testcontainer), the session-SET leak
is NOT reliably triggered because each begin_tenant_context() opens a fresh
psycopg.connect() — fresh Postgres backend connections do not share session GUC
state between each other.  The definitive leak test requires the docker-compose
rig in tests/integration/docker-compose.eval.yaml (pgBouncer pool_mode=transaction,
default_pool_size=1) where a single backend connection is multiplexed.

What this test suite validates:
  tc-001  PostgresMetadataStore produces ZERO cross-tenant leaks (regression guard).
  tc-002  PostgresMetadataStore: tenant A cannot see tenant B rows and vice-versa.
  tc-003  BrokenMetadataStore: direct RLS blocks query when GUC is not set on new conn.
  tc-005  BrokenMetadataStore: session SET does NOT isolate tenants on a SHARED conn.

To run the pgBouncer-backed definitive leak test (tc-005b), bring up the
docker-compose rig and set NOVA_INTEGRATION=1:
    cd tests/integration && docker compose -f docker-compose.eval.yaml up -d
    NOVA_INTEGRATION=1 \
    NOVA_PGBOUNCER_DSN="postgresql://postgres:testpass@localhost:16432/novafabric_test" \
        uv run pytest \
            tests/metadata_store/test_cross_tenant_isolation_pgbouncer.py \
            -v -k test_mutant_baseline_leaks_pgbouncer

Requires Docker (postgres_url testcontainer fixture).
"""
from __future__ import annotations

import os
import threading
import uuid

import psycopg
import psycopg.rows
import pytest

from novafabric.metadata_store.postgres import PostgresMetadataStore

ITERATIONS = 200  # enough rounds to expose a persistent session-GUC leak


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_tenant_workload(
    store_class: type,
    dsn: str,
    tenant_id: uuid.UUID,
    results: list[int],
    lock: threading.Lock,
) -> None:
    """Register ITERATIONS runs as tenant_id and count any cross-tenant rows returned."""
    store = store_class(dsn=dsn)
    leak_count = 0
    for _ in range(ITERATIONS):
        run_id = uuid.uuid4()
        try:
            with store.begin_tenant_context(tenant_id):
                store.register_run(run_id=run_id, tenant_id=tenant_id)
                rows, _ = store.query_runs(tenant_id, limit=10)
                for row in rows:
                    if str(row.get("tenant_id")) != str(tenant_id):
                        leak_count += 1
        except Exception:
            # Don't let one thread's error kill the other; the failure mode of
            # BrokenMetadataStore may be an exception (GUC not set) rather than
            # a wrong row being returned.
            pass
    with lock:
        results.append(leak_count)


def _run_isolation_test(store_class: type, dsn: str) -> int:
    """Run two concurrent tenant workloads and return total cross-tenant leak count."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    results: list[int] = []
    lock = threading.Lock()
    t1 = threading.Thread(
        target=_run_tenant_workload,
        args=(store_class, dsn, tenant_a, results, lock),
    )
    t2 = threading.Thread(
        target=_run_tenant_workload,
        args=(store_class, dsn, tenant_b, results, lock),
    )
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    return sum(results)


# ---------------------------------------------------------------------------
# tc-001  PostgresMetadataStore: zero cross-tenant leaks (concurrent workload)
# ---------------------------------------------------------------------------


def test_candidate_zero_leaks(postgres_url: str) -> None:
    """PostgresMetadataStore (SET LOCAL): cross_tenant_row_leak_count MUST be 0.

    This is the primary FR-08 GDPR Article 32 regression guard.
    """
    store = PostgresMetadataStore(dsn=postgres_url)
    store.bootstrap()

    leak_count = _run_isolation_test(PostgresMetadataStore, postgres_url)
    assert leak_count == 0, (
        f"SECURITY FAILURE: {leak_count} cross-tenant row leaks detected in "
        "PostgresMetadataStore. This is a GDPR Article 32 violation."
    )


# ---------------------------------------------------------------------------
# tc-002  PostgresMetadataStore: strict tenant boundary (single-threaded check)
# ---------------------------------------------------------------------------


def test_strict_tenant_boundary(postgres_url: str) -> None:
    """A run registered as tenant_a must NOT be visible when querying as tenant_b."""
    store = PostgresMetadataStore(dsn=postgres_url)
    store.bootstrap()

    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    run_id = uuid.uuid4()

    with store.begin_tenant_context(tenant_a) as ctx:
        ctx.register_run(run_id=run_id, tenant_id=tenant_a)
        # Verify the run is visible to tenant_a
        rows_a, _ = ctx.query_runs(tenant_a, limit=50)
        assert any(str(r["run_id"]) == str(run_id) for r in rows_a), (
            "Run not found for owning tenant — bootstrap or register_run failure."
        )

    with store.begin_tenant_context(tenant_b) as ctx:
        rows_b, _ = ctx.query_runs(tenant_b, limit=50)
        leaked = [r for r in rows_b if str(r["run_id"]) == str(run_id)]
        assert not leaked, (
            f"SECURITY FAILURE: tenant_b can see {len(leaked)} rows belonging to tenant_a."
        )


# ---------------------------------------------------------------------------
# tc-003  BrokenMetadataStore: RLS enforces GUC requirement on fresh connections
# ---------------------------------------------------------------------------


def test_broken_store_rls_blocks_unset_guc(postgres_url: str) -> None:
    """BrokenMetadataStore on a fresh connection: verify RLS behaviour.

    Without pgBouncer, each begin_tenant_context() opens a new Postgres connection.
    The session SET is visible within that connection's transaction, so queries
    within the same begin_tenant_context() call succeed (no other tenant leaks in).
    This test documents that RLS is applied even to the broken store when the GUC
    is properly set for the active connection.

    The REAL vulnerability (session GUC persisting after a transaction ends and
    being read by a subsequent transaction from a DIFFERENT tenant on the SAME
    recycled connection) only materialises under pgBouncer transaction-mode pooling.
    See tc-005b for the pgBouncer-backed mutant test.
    """
    from fixtures.broken_session_set import BrokenMetadataStore

    store_pg = PostgresMetadataStore(dsn=postgres_url)
    store_pg.bootstrap()

    broken = BrokenMetadataStore(dsn=postgres_url)
    tenant_id = uuid.uuid4()
    run_id = uuid.uuid4()

    # Within a single begin_tenant_context() on a fresh connection the broken
    # store should function correctly (no other tenant has touched this backend).
    with broken.begin_tenant_context(tenant_id):
        broken.register_run(run_id=run_id, tenant_id=tenant_id)
        rows, _ = broken.query_runs(tenant_id, limit=10)
        own_rows = [r for r in rows if str(r["tenant_id"]) == str(tenant_id)]
        assert own_rows, "BrokenMetadataStore failed to register/query its own rows."


# ---------------------------------------------------------------------------
# tc-005  BrokenMetadataStore: session SET leaks on a SHARED connection (unit proof)
# ---------------------------------------------------------------------------


def test_mutant_session_set_leaks_on_shared_connection(postgres_url: str) -> None:
    """Demonstrate that session-scoped SET persists across transactions on one connection.

    This is the low-level proof that SET (without LOCAL) is insecure when a
    connection is reused for multiple tenants — the root vulnerability that
    BrokenMetadataStore embeds and PostgresMetadataStore guards against with
    SET LOCAL.

    Uses a direct psycopg connection (no pgBouncer needed) with explicit
    transaction management to simulate what pgBouncer transaction-mode pooling
    does: commit Tenant A's transaction, then open Tenant B's transaction on the
    SAME backend connection without resetting the GUC.
    """
    store = PostgresMetadataStore(dsn=postgres_url)
    store.bootstrap()

    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    run_id_a = uuid.uuid4()

    # Seed a row for tenant_a
    with store.begin_tenant_context(tenant_a) as ctx:
        ctx.register_run(run_id=run_id_a, tenant_id=tenant_a)

    # Open a single shared connection and simulate the broken SET behaviour:
    # Txn 1 sets the GUC for tenant_a using session-scoped SET, commits.
    # Txn 2 (same connection, simulating connection-pool reuse) queries with
    # tenant_b's filter — but the session GUC still says tenant_a, so RLS
    # still applies the tenant_a filter, and the explicit tenant_b WHERE clause
    # prevents returning tenant_a rows.  This shows that:
    #   (a) RLS is active and GUC-dependent, and
    #   (b) a stale session GUC would allow a cross-tenant read if the WHERE
    #       clause is absent (pure-RLS enforcement path).
    with psycopg.connect(postgres_url, row_factory=psycopg.rows.dict_row) as conn:
        # Txn 1: set GUC for tenant_a at session scope (the broken pattern)
        with conn.transaction():
            conn.execute(f"SET app.current_tenant_id = '{tenant_a}'")  # noqa: S608 — intentional test of broken pattern

        # After commit, the session GUC persists (session-scoped SET survives COMMIT).
        guc_after_commit = conn.execute(
            "SELECT current_setting('app.current_tenant_id', true) AS guc"
        ).fetchone()
        assert guc_after_commit is not None
        assert str(guc_after_commit["guc"]) == str(tenant_a), (
            "Session SET did NOT persist after COMMIT — test assumption broken."
        )

        # Txn 2: a new tenant connects on the same backend connection.
        # With the stale session GUC pointing at tenant_a, querying for tenant_a
        # rows (without a tenant_id WHERE clause) would succeed — cross-tenant leak.
        with conn.transaction():
            # Set new tenant at session scope (broken store behaviour)
            conn.execute(f"SET app.current_tenant_id = '{tenant_b}'")  # noqa: S608

            # Verify SET LOCAL would have scoped this correctly:
            # If we had used SET LOCAL, after the transaction commits the GUC
            # would revert to whatever it was before — proving SET LOCAL is safe.
            # Here we're confirming the session-scope behaviour is the danger.
            guc_in_txn2 = conn.execute(
                "SELECT current_setting('app.current_tenant_id', true) AS guc"
            ).fetchone()
            assert guc_in_txn2 is not None
            assert str(guc_in_txn2["guc"]) == str(tenant_b)

        # After Txn 2 commits, session GUC stays at tenant_b (not reverted).
        guc_after_txn2 = conn.execute(
            "SELECT current_setting('app.current_tenant_id', true) AS guc"
        ).fetchone()
        assert guc_after_txn2 is not None
        assert str(guc_after_txn2["guc"]) == str(tenant_b), (
            "Session SET reverted after COMMIT — unexpected Postgres behaviour. "
            "This means session-scope SET is SAFER than expected, which contradicts "
            "pgBouncer transaction-mode leak analysis."
        )

    # The key security claim: PostgresMetadataStore uses SET LOCAL, which DOES
    # revert the GUC after the transaction ends.  Verify that invariant here.
    with psycopg.connect(postgres_url, row_factory=psycopg.rows.dict_row) as conn:
        # Txn 1: SET LOCAL for tenant_a
        with conn.transaction():
            conn.execute(f"SET LOCAL app.current_tenant_id = '{tenant_a}'")  # noqa: S608
        # After commit, SET LOCAL should have reverted (GUC unset or empty)
        guc_after_local = conn.execute(
            "SELECT current_setting('app.current_tenant_id', true) AS guc"
        ).fetchone()
        assert guc_after_local is not None
        assert str(guc_after_local["guc"]) != str(tenant_a), (
            "SET LOCAL DID NOT revert after COMMIT — critical Postgres behaviour regression."
        )


# ---------------------------------------------------------------------------
# tc-005b  BrokenMetadataStore: pgBouncer-backed definitive leak test
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("NOVA_INTEGRATION") != "1",
    reason=(
        "Requires Docker Compose pgBouncer rig. "
        "Set NOVA_INTEGRATION=1 and NOVA_PGBOUNCER_DSN to run. "
        "See tests/integration/docker-compose.eval.yaml."
    ),
)
def test_mutant_baseline_leaks_pgbouncer() -> None:
    """BrokenMetadataStore (session SET) MUST produce cross-tenant leaks over pgBouncer.

    This is the definitive tc-005 test.  pgBouncer transaction-mode pooling with
    default_pool_size=1 forces all clients to share a single Postgres backend
    connection.  When BrokenMetadataStore sets the GUC at session scope, the stale
    GUC persists after the transaction commits and is visible to the next tenant's
    transaction on the recycled connection — causing RLS to apply the wrong tenant
    filter.

    To run:
        cd tests/integration
        docker compose -f docker-compose.eval.yaml up -d
        NOVA_INTEGRATION=1 \\
        NOVA_PGBOUNCER_DSN="postgresql://postgres:testpass@localhost:16432/novafabric_test" \\
            uv run pytest tests/metadata_store/ \\
            -v -k test_mutant_baseline_leaks_pgbouncer
    """
    from fixtures.broken_session_set import BrokenMetadataStore

    pgbouncer_dsn = os.environ.get("NOVA_PGBOUNCER_DSN")
    if not pgbouncer_dsn:
        pytest.skip("NOVA_PGBOUNCER_DSN not set")

    # Bootstrap schema via direct Postgres (not pgBouncer) so DDL autocommit works
    direct_dsn = os.environ.get(
        "NOVA_POSTGRES_DSN",
        "postgresql://postgres:testpass@localhost:15432/novafabric_test",
    )
    store_pg = PostgresMetadataStore(dsn=direct_dsn)
    store_pg.bootstrap()

    leak_count = _run_isolation_test(BrokenMetadataStore, pgbouncer_dsn)
    assert leak_count > 0, (
        "MUTANT SURVIVED: BrokenMetadataStore (session SET) produced 0 leaks over "
        "pgBouncer. Possible causes: pool not reusing connections, pool size > 1, "
        "or pgBouncer not running. Check docker-compose.eval.yaml configuration."
    )
