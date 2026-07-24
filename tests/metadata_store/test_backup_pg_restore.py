"""ADR-0217 integration: real pg_dump → nova restore → real pg_restore cycle.

Runs against the shared testcontainers Postgres; skips without Docker or the
Postgres client tools. The full contract: seed the source DB, take a pg-dump
backup set, restore into a SECOND database in the same container, and prove
counts match the manifest, alembic sits at head, and RLS is enforced.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from novafabric.backup.create import create_backup  # noqa: E402
from novafabric.backup.restore import restore_backup  # noqa: E402
from novafabric.metadata_store.cli import run_alembic_upgrade  # noqa: E402
from novafabric.metadata_store.postgres import PostgresMetadataStore  # noqa: E402


def _require_pg_client_tools() -> None:
    for tool in ("pg_dump", "pg_restore"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} not on PATH — skipping pg restore integration test")


@pytest.fixture()
def restore_target_dsn(postgres_url: str) -> str:
    """A second, empty database in the same container."""
    dbname = f"restore_target_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{dbname}"')
    head, _, _ = postgres_url.rpartition("/")
    return f"{head}/{dbname}"


def test_full_pg_backup_restore_cycle(
    postgres_url: str, restore_target_dsn: str, tmp_path: Path, monkeypatch
) -> None:
    _require_pg_client_tools()
    monkeypatch.setattr("novafabric.backup.create.load_signing_profile", lambda: None)

    # Version-skew guard: a pg_dump older than the server cannot dump it.
    proc = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True)
    client_major = int(proc.stdout.split()[-1].split(".")[0])
    if client_major < 16:
        pytest.skip(f"pg_dump major {client_major} < server 16 — cannot dump")

    # --- seed the source DB via the real store --------------------------------
    ok, detail = run_alembic_upgrade("postgres", dsn=postgres_url)
    assert ok, detail
    store = PostgresMetadataStore(postgres_url)
    store.bootstrap()
    tenant = uuid.uuid4()
    run_ids = [uuid.uuid4() for _ in range(3)]
    with store.begin_tenant_context(tenant) as scoped:
        for run_id in run_ids:
            scoped.register_run(run_id, tenant)

    # --- home with a registry so the set has home-side members too ------------
    from novafabric.registry.store import get_connection, init_schema

    home = tmp_path / "pg-home"
    home.mkdir()
    conn = get_connection(home / "registry.db")
    init_schema(conn)
    conn.close()

    # --- create + restore ------------------------------------------------------
    result = create_backup(
        tmp_path / "pg-set.tar.gz", home=home, profile="pg", dsn=postgres_url
    )
    manifest = result.manifest
    assert manifest.pg_table_counts is not None
    assert manifest.pg_table_counts["runs"] == 3
    assert manifest.pg_schema_revision is not None
    assert postgres_url not in (manifest.db_target or "")

    target_home = tmp_path / "restored-home"
    out = restore_backup(result.archive_path, home=target_home, dsn=restore_target_dsn)
    assert out.ok is True, [s for s in out.steps if not s.ok]

    by_name = {s.name: s for s in out.steps}
    assert by_name["pg-restore"].ok
    assert by_name["pg-migrations"].ok
    assert "3" in by_name["check-target-db"].detail or by_name["check-target-db"].ok
    assert "match the manifest" in by_name["verify-db-counts"].detail
    assert "tenant_isolation verified" in by_name["verify-rls"].detail

    # --- read the restored DB through the real store ---------------------------
    restored = PostgresMetadataStore(restore_target_dsn)
    with restored.begin_tenant_context(tenant) as scoped:
        for run_id in run_ids:
            assert scoped.lookup_run(run_id, tenant) is not None

    with psycopg.connect(restore_target_dsn) as conn2:
        row = conn2.execute("SELECT version_num FROM alembic_version").fetchone()
        assert row is not None  # migrations at a real revision


def test_pg_restore_refuses_restoring_over_source_without_force(
    postgres_url: str, tmp_path: Path, monkeypatch
) -> None:
    _require_pg_client_tools()
    monkeypatch.setattr("novafabric.backup.create.load_signing_profile", lambda: None)
    from novafabric.backup.restore import RestoreError
    from novafabric.registry.store import get_connection, init_schema

    proc = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True)
    if int(proc.stdout.split()[-1].split(".")[0]) < 16:
        pytest.skip("pg client older than server")

    ok, detail = run_alembic_upgrade("postgres", dsn=postgres_url)
    assert ok, detail
    home = tmp_path / "home"
    home.mkdir()
    conn = get_connection(home / "registry.db")
    init_schema(conn)
    conn.close()
    result = create_backup(
        tmp_path / "set.tar.gz", home=home, profile="pg", dsn=postgres_url
    )
    # The source DB is non-empty; restoring over it must be refused w/o force.
    with pytest.raises(RestoreError, match="not empty"):
        restore_backup(result.archive_path, home=tmp_path / "h2", dsn=postgres_url)
