"""FR-11: Two-role split audit."""
from __future__ import annotations

import psycopg

from novafabric.metadata_store.postgres import PostgresMetadataStore
from novafabric.metadata_store.rls import verify_role_split


def test_role_split_bypassrls(postgres_url: str) -> None:
    store = PostgresMetadataStore(dsn=postgres_url)
    store.bootstrap()
    with psycopg.connect(postgres_url) as conn:
        result = verify_role_split(conn)
    assert "novafabric_app_bypassrls" in result, "novafabric_app role not found"
    assert "novafabric_migrator_bypassrls" in result, "novafabric_migrator role not found"
    assert result["novafabric_app_bypassrls"] is False, "novafabric_app must NOT have BYPASSRLS"
    assert result["novafabric_migrator_bypassrls"] is True, (
        "novafabric_migrator must have BYPASSRLS"
    )
