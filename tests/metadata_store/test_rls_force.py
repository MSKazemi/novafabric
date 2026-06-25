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
