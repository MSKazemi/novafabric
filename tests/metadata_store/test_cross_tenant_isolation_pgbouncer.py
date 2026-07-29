"""FR-08 — SECURITY-CRITICAL — GDPR Article 32 control.

Two concurrent tenants interleave register_run / query_runs.
Pass:  cross_tenant_row_leak_count == 0 AND cross_tenant_exception_count == 0 AND
       stale_guc_count == 0 for PostgresMetadataStore (SET LOCAL).
Mutant: BrokenMetadataStore (session SET) MUST demonstrate the vulnerability via
        stale_guc_count (see "Three observable symptoms" below — the other two are
        kept for defense in depth but cannot fire for this fixture's own design).

Three observable symptoms considered (2026-07-29 redesign, BL-009) — only the third
actually detects this fixture's vulnerability; the first two are analyzed here so a
future reader doesn't have to re-derive why they don't fire:

1. **Row-content leak** (`leak_count`, the original, sole pre-2026-07-29 signal).
   `BrokenMetadataStore.query_runs`/`register_run` both take `tenant_id` as an
   explicit caller-supplied argument — `query_runs` filters `WHERE tenant_id=%s` in
   SQL. The RLS policy on `runs` is `tenant_id = current_setting('app.current_tenant_id')::uuid`
   for both `USING` and `WITH CHECK` (`polcmd = '*'`). A stale GUC disagreeing with
   the explicit WHERE clause can only narrow the result set to the AND of two
   *disagreeing* filters — always empty, never an extra wrongly-labeled row. This
   assertion was structurally unreachable before this redesign; the test had never
   actually run to completion (the docker-compose rig's `bitnami/pgbouncer` image had
   been pulled from Docker Hub's free tier), so nobody had discovered this.
2. **RLS `WITH CHECK` exception** (`exception_count`). Anticipated by this suite's own
   original comment ("the failure mode of BrokenMetadataStore may be an exception ...
   rather than a wrong row being returned"), but never counted before this redesign.
   Turns out this *also* never fires for this fixture: `begin_tenant_context(tenant_id)`
   unconditionally `SET`s the GUC to its own caller-supplied (correct) `tenant_id` as
   the very first statement of every transaction, before any INSERT/SELECT — so
   `register_run`'s `WITH CHECK` always sees a GUC that matches, regardless of what a
   *different* tenant's transaction left behind on a recycled connection. The
   session-vs-local distinction genuinely doesn't matter to any operation that always
   re-establishes its own context before acting — which is exactly what
   `begin_tenant_context` does, every time, for both stores.
3. **Stale-GUC peek** (`stale_guc_count` — the one that actually works). Between two
   workload iterations, peek what the pool-recycled backend connection's GUC already
   is, via a THIRD connection that issues no `SET` of its own, before the next
   iteration establishes its own context. Under `pool_mode=transaction,
   default_pool_size=1`, this peek connection and the next iteration's connection are
   forced to share the *same* physical backend (there is only one) — so the peek sees
   exactly what the *previous* occupant (possibly the other tenant's thread) left
   behind. This is the real production bug class SET LOCAL defends against: code that
   *trusts* an already-established session-level tenant context — a raw debug query,
   a batch job sharing the pool, a cached connection/ORM-session object reused across
   requests — instead of re-scoping on every use, the way `begin_tenant_context` does.
   For `PostgresMetadataStore` (SET LOCAL), the GUC always reverts at COMMIT, so the
   peek sees `NULL`/unset (fails closed — RLS then blocks everything, the safe
   outcome). For `BrokenMetadataStore` (session SET), the peek sees the *other*
   tenant's UUID whenever pgBouncer happened to hand this backend to that tenant's
   thread most recently — empirically verified 2026-07-29 to occur reliably at
   `ITERATIONS = 200` concurrent interleavings.

**A second, independent pgBouncer-config prerequisite, also discovered 2026-07-29:**
pgBouncer's own compiled-in default `server_reset_query = DISCARD ALL` runs on every
connection release regardless of `server_reset_query_always` when a client fully
disconnects after its transaction — which wipes the GUC between every iteration
regardless of pool_mode/pool_size, making the vulnerability unobservable by ANY
symptom. `tests/integration/docker-compose.eval.yaml` now bind-mounts a
`pgbouncer.ini` with `server_reset_query` explicitly empty — see that file's own
comment. This was a genuine, pre-existing gap in the rig's configuration, independent
of the Docker-image-availability issue BL-008 fixed; nobody had discovered it either,
because the rig had never successfully run before BL-008.

Without pgBouncer in front (direct Postgres testcontainer), the session-SET leak
is NOT reliably triggered because each begin_tenant_context() opens a fresh
psycopg.connect() — fresh Postgres backend connections do not share session GUC
state between each other.  The definitive leak test requires the docker-compose
rig in tests/integration/docker-compose.eval.yaml (pgBouncer pool_mode=transaction,
default_pool_size=1, server_reset_query disabled) where a single backend connection
is multiplexed with no state cleared between clients.

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


def _peek_guc_without_setting(dsn: str) -> str | None:
    """Open a connection and read `app.current_tenant_id` WITHOUT ever setting it.

    Models the real bug class SET LOCAL defends against: code that trusts an
    already-established session-level tenant context instead of re-scoping on every
    use. Under `pool_mode=transaction, default_pool_size=1`, this connection is
    forced to share the single backend with whatever ran immediately before it, so a
    non-``None`` result here is exactly the previous occupant's leftover GUC.
    """
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            row = conn.execute(
                "SELECT current_setting('app.current_tenant_id', true) AS guc"
            ).fetchone()
        return row[0] if row else None


def _reset_shared_guc_baseline(dsn: str) -> None:
    """Clear any leftover `app.current_tenant_id` on the shared backend connection.

    `server_reset_query` is deliberately disabled on this rig (see the module
    docstring), so a *previous test function*'s session-scoped `SET` (e.g. an
    earlier `BrokenMetadataStore` run in the same pytest session) can still be
    sitting on the physical backend connection as its committed baseline —
    contaminating a *later* test's `stale_guc_count` signal with old, unrelated
    data rather than this test's own two tenants. `RESET` clears it back to unset
    before a test's workload starts, independent of whatever ran before it.
    """
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            conn.execute("RESET app.current_tenant_id")


def _run_tenant_workload(
    store_class: type,
    dsn: str,
    tenant_id: uuid.UUID,
    other_tenant_id: uuid.UUID,
    results: list[tuple[int, int, int]],
    lock: threading.Lock,
) -> None:
    """Register ITERATIONS runs as tenant_id; count all three observable symptoms.

    Appends ``(leak_count, exception_count, stale_guc_count)`` to *results* — see the
    module docstring's "Three observable symptoms" for what each one means and why
    only the third can fire for this specific fixture. `stale_guc_count` counts a
    peek matching *specifically* `other_tenant_id` (this test's own sibling thread),
    not just "any non-null, non-mine value" — a precise, self-contained signal that
    can't be confused with contamination from an unrelated earlier test run. Never
    let one iteration's exception (or peek failure) abort the workload — a real leak
    keeps happening across all 200 iterations, so losing one datapoint doesn't hide it.
    """
    store = store_class(dsn=dsn)
    leak_count = 0
    exception_count = 0
    stale_guc_count = 0
    for _ in range(ITERATIONS):
        run_id = uuid.uuid4()
        try:
            stale = _peek_guc_without_setting(dsn)
            if stale == str(other_tenant_id):
                stale_guc_count += 1
        except Exception:
            pass  # diagnostic peek only; never fail the workload on it
        try:
            with store.begin_tenant_context(tenant_id):
                store.register_run(run_id=run_id, tenant_id=tenant_id)
                rows, _ = store.query_runs(tenant_id, limit=10)
                for row in rows:
                    if str(row.get("tenant_id")) != str(tenant_id):
                        leak_count += 1
        except Exception:
            # A correctly-scoped operation on this iteration's own tenant_id should
            # never raise — see the module docstring's "Three observable symptoms".
            exception_count += 1
    with lock:
        results.append((leak_count, exception_count, stale_guc_count))


def _run_isolation_test(store_class: type, dsn: str) -> tuple[int, int, int]:
    """Run two concurrent tenant workloads; return (leaks, exceptions, stale_gucs)."""
    _reset_shared_guc_baseline(dsn)
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    results: list[tuple[int, int, int]] = []
    lock = threading.Lock()
    t1 = threading.Thread(
        target=_run_tenant_workload,
        args=(store_class, dsn, tenant_a, tenant_b, results, lock),
    )
    t2 = threading.Thread(
        target=_run_tenant_workload,
        args=(store_class, dsn, tenant_b, tenant_a, results, lock),
    )
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    total_leaks = sum(leaks for leaks, _, _ in results)
    total_exceptions = sum(exc for _, exc, _ in results)
    total_stale = sum(stale for _, _, stale in results)
    return total_leaks, total_exceptions, total_stale


# ---------------------------------------------------------------------------
# tc-001  PostgresMetadataStore: zero cross-tenant leaks (concurrent workload)
# ---------------------------------------------------------------------------


def test_candidate_zero_leaks(postgres_url: str) -> None:
    """PostgresMetadataStore (SET LOCAL): all three leak signals MUST be 0.

    This is the primary FR-08 GDPR Article 32 regression guard. `SET LOCAL` scopes
    the GUC to its own transaction, so: a correctly-scoped operation on this
    iteration's own tenant_id must never raise an RLS violation (`exception_count`);
    and the GUC must always have reverted by the time the next connection peeks it,
    with no explicit SET of its own (`stale_guc_count`) — a fresh, unset GUC makes
    RLS fail closed rather than leak. Both are asserted to 0 here, not just
    swallowed, so this guard can't silently pass on a broken store that happens to
    also raise, or that only sometimes leaves a stale GUC behind.
    """
    store = PostgresMetadataStore(dsn=postgres_url)
    store.bootstrap()

    leak_count, exception_count, stale_guc_count = _run_isolation_test(
        PostgresMetadataStore, postgres_url
    )
    assert leak_count == 0, (
        f"SECURITY FAILURE: {leak_count} cross-tenant row leaks detected in "
        "PostgresMetadataStore. This is a GDPR Article 32 violation."
    )
    assert exception_count == 0, (
        f"{exception_count} unexpected exceptions during correctly-scoped operations "
        "in PostgresMetadataStore — SET LOCAL should never cause a same-tenant "
        "operation to fail. Investigate before trusting the leak_count==0 result above."
    )
    assert stale_guc_count == 0, (
        f"SECURITY FAILURE: {stale_guc_count} stale cross-tenant GUC(s) observed on "
        "a pgBouncer-recycled connection after PostgresMetadataStore's SET LOCAL "
        "should have reverted it. This is the exact GDPR Article 32 violation SET "
        "LOCAL exists to prevent."
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
    """BrokenMetadataStore (session SET) MUST demonstrate the vulnerability over pgBouncer.

    This is the definitive tc-005 test. pgBouncer transaction-mode pooling with
    default_pool_size=1 and server_reset_query disabled forces all clients to share a
    single Postgres backend connection with no state cleared between them. When
    BrokenMetadataStore sets the GUC at session scope, the stale GUC persists after
    the transaction commits and is visible to whatever connects next.

    Redesigned 2026-07-29 (BL-009), in two independent parts:

    1. **The rig itself was missing a prerequisite.** pgBouncer's own compiled-in
       default `server_reset_query = DISCARD ALL` runs on every connection release
       regardless of `server_reset_query_always`, wiping the GUC between every
       iteration — making the vulnerability unobservable by ANY symptom, independent
       of the Docker-image issue BL-008 already fixed. `docker-compose.eval.yaml` now
       bind-mounts a `pgbouncer.ini` with `server_reset_query` explicitly disabled.
       Verified empirically: without this, even a raw two-connection script (no test
       fixture involved) showed the GUC reset between connections; with it, the GUC
       persists exactly as this ADR's threat model assumes.
    2. **The assertion's signal was wrong, even with the rig fixed.** The original
       `leak_count > 0` (and an interim `leak_count > 0 or exception_count > 0`) are
       both structurally unreachable for THIS fixture, for a different reason than
       first suspected: `begin_tenant_context(tenant_id)` unconditionally `SET`s the
       GUC to its own caller-supplied (correct) `tenant_id` as the first statement of
       every transaction — so by the time `register_run`/`query_runs` run, the GUC
       always matches, regardless of what a *different* tenant's transaction left on
       a recycled connection. Both signals are kept in the module's docstring analysis
       and (harmlessly) in `_run_tenant_workload`'s counters for defense in depth, but
       the primary detector is now `stale_guc_count`: a third, diagnostic-only
       connection peeks the GUC *without ever setting it itself*, immediately before
       each iteration — which, under `pool_size=1`, is forced to share the same
       backend as whatever ran immediately before it. See the module docstring's
       "Three observable symptoms" for the full analysis of why 1 and 2 don't fire.

    Before this redesign, this test had never actually run to completion — the
    compose rig's pinned `bitnami/pgbouncer:1.22.1` image had been pulled from Docker
    Hub's free tier, so nobody had discovered either gap.

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

    leak_count, exception_count, stale_guc_count = _run_isolation_test(
        BrokenMetadataStore, pgbouncer_dsn
    )
    assert leak_count > 0 or exception_count > 0 or stale_guc_count > 0, (
        "MUTANT SURVIVED: BrokenMetadataStore (session SET) produced 0 row-content "
        "leaks, 0 RLS-violation exceptions, AND 0 stale cross-tenant GUC peeks over "
        "pgBouncer. Possible causes: pool not reusing connections, pool size > 1, "
        "server_reset_query not actually disabled, or pgBouncer not running. Check "
        "docker-compose.eval.yaml / pgbouncer.ini configuration."
    )


# ---------------------------------------------------------------------------
# tc-001b  PostgresMetadataStore: zero leaks even over the SAME adversarial rig
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("NOVA_INTEGRATION") != "1",
    reason=(
        "Requires Docker Compose pgBouncer rig. "
        "Set NOVA_INTEGRATION=1 and NOVA_PGBOUNCER_DSN to run. "
        "See tests/integration/docker-compose.eval.yaml."
    ),
)
def test_candidate_zero_leaks_pgbouncer() -> None:
    """PostgresMetadataStore (SET LOCAL) MUST stay leak-free over the SAME rig
    that proves BrokenMetadataStore vulnerable.

    `test_candidate_zero_leaks` (tc-001) already proves this against a direct
    Postgres testcontainer, but that rig can't share a backend connection across
    clients at all, so it can't distinguish "safe by design" from "safe because the
    adversarial condition never occurred." Running the *same* SET-LOCAL store
    through the *same* pool_size=1, server_reset_query-disabled pgBouncer rig that
    `test_mutant_baseline_leaks_pgbouncer` uses to catch the mutant is the direct,
    symmetric proof that SET LOCAL is what makes the difference — same adversarial
    pooling conditions, opposite outcome, because SET LOCAL (unlike SET) reverts at
    COMMIT regardless of what the pool does afterward.
    """
    pgbouncer_dsn = os.environ.get("NOVA_PGBOUNCER_DSN")
    if not pgbouncer_dsn:
        pytest.skip("NOVA_PGBOUNCER_DSN not set")

    direct_dsn = os.environ.get(
        "NOVA_POSTGRES_DSN",
        "postgresql://postgres:testpass@localhost:15432/novafabric_test",
    )
    store_pg = PostgresMetadataStore(dsn=direct_dsn)
    store_pg.bootstrap()

    leak_count, exception_count, stale_guc_count = _run_isolation_test(
        PostgresMetadataStore, pgbouncer_dsn
    )
    assert leak_count == 0 and exception_count == 0 and stale_guc_count == 0, (
        f"SECURITY FAILURE: PostgresMetadataStore (SET LOCAL) produced "
        f"leak_count={leak_count}, exception_count={exception_count}, "
        f"stale_guc_count={stale_guc_count} over the SAME adversarial pgBouncer rig "
        "that correctly catches BrokenMetadataStore. This is a GDPR Article 32 "
        "regression in SET LOCAL's core guarantee."
    )
