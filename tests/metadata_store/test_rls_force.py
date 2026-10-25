"""FR-09 + FR-10: FORCE ROW LEVEL SECURITY + canonical policy text."""
from __future__ import annotations

import psycopg

from novafabric.metadata_store.postgres import PostgresMetadataStore
from novafabric.metadata_store.rls import TENANT_SCOPED_TABLES, verify_force_rls, verify_policy_text


def test_force_rls_all_tables(postgres_url: str) -> None:
    store = PostgresMetadataStore(dsn=postgres_url)
    store.bootstrap()
    with psycopg.connect(postgres_url) as conn:
        results = verify_force_rls(conn, TENANT_SCOPED_TABLES)
    missing = [t for t, v in results.items() if not v]
    assert not missing, f"FORCE RLS not set on: {missing}"
    assert set(results.keys()) == set(TENANT_SCOPED_TABLES)


def test_canonical_policy_text(postgres_url: str) -> None:
    store = PostgresMetadataStore(dsn=postgres_url)
    store.bootstrap()
    with psycopg.connect(postgres_url) as conn:
        results = verify_policy_text(conn, TENANT_SCOPED_TABLES)
    mismatches = [t for t, ok in results.items() if not ok]
    assert not mismatches, f"Canonical policy text mismatch on: {mismatches}"
    assert set(results.keys()) == set(TENANT_SCOPED_TABLES)


# ── review storage-backup-19 ────────────────────────────────────────────────


def test_force_without_enable_is_not_row_security(postgres_url: str) -> None:
    """FORCE only extends an enabled policy set; FORCE alone enforces nothing."""
    PostgresMetadataStore(dsn=postgres_url).bootstrap()
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        conn.execute("ALTER TABLE runs DISABLE ROW LEVEL SECURITY")  # FORCE flag stays set
        results = verify_force_rls(conn, TENANT_SCOPED_TABLES)
    assert results["runs"] is False
    assert all(results[t] for t in TENANT_SCOPED_TABLES if t != "runs")


def test_a_drifted_with_check_fails_the_policy_check(postgres_url: str) -> None:
    PostgresMetadataStore(dsn=postgres_url).bootstrap()
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        conn.execute("ALTER POLICY tenant_isolation ON capsules WITH CHECK (true)")
        results = verify_policy_text(conn, TENANT_SCOPED_TABLES)
    assert results["capsules"] is False, "WITH CHECK (true) lets a tenant write into another"
    assert all(results[t] for t in TENANT_SCOPED_TABLES if t != "capsules")


def test_restore_reports_drift_it_repaired(postgres_url: str) -> None:
    from novafabric.backup.restore import _verify_pg_rls

    PostgresMetadataStore(dsn=postgres_url).bootstrap()
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        conn.execute("ALTER TABLE signatures DISABLE ROW LEVEL SECURITY")
        conn.execute("ALTER POLICY tenant_isolation ON runs WITH CHECK (true)")

    step = _verify_pg_rls(postgres_url)

    assert step.ok, step.detail
    assert "DRIFT" in step.detail and "signatures" in step.detail and "runs" in step.detail
    with psycopg.connect(postgres_url) as conn:
        assert all(verify_force_rls(conn, TENANT_SCOPED_TABLES).values())
        assert all(verify_policy_text(conn, TENANT_SCOPED_TABLES).values())


def test_restore_of_a_healthy_database_reports_no_drift(postgres_url: str) -> None:
    from novafabric.backup.restore import _verify_pg_rls

    PostgresMetadataStore(dsn=postgres_url).bootstrap()
    step = _verify_pg_rls(postgres_url)
    assert step.ok, step.detail
    assert "DRIFT" not in step.detail
