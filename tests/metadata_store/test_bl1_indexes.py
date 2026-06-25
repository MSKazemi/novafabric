"""Tests for BL-1: SQL indexes on hot query paths.

Verifies that:
- _ensure_indexes() is idempotent (safe to call twice, no error on second call)
- Indexes exist on the SQLite DB after bootstrap()
- The DuckDB evidence fabric accumulator creates from_ref/to_ref indexes
"""
from __future__ import annotations

import sqlite3

import pytest

from novafabric.metadata_store.sqlite import SQLiteMetadataStore

# ---------------------------------------------------------------------------
# SQLiteMetadataStore — index idempotency
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A bootstrapped SQLiteMetadataStore using a temp DB."""
    monkeypatch.delenv("NOVAFABRIC_API_WORKERS", raising=False)
    s = SQLiteMetadataStore(db_path=tmp_path / "meta.db")
    s.bootstrap()
    return s


def test_ensure_indexes_idempotent(tmp_path, monkeypatch):
    """Calling _ensure_indexes() twice must not raise any error."""
    monkeypatch.delenv("NOVAFABRIC_API_WORKERS", raising=False)
    s = SQLiteMetadataStore(db_path=tmp_path / "idem.db")
    s.bootstrap()
    # First call is done inside bootstrap(); calling again must be a no-op
    s._ensure_indexes()
    s._ensure_indexes()


def test_ensure_indexes_creates_runs_started_at(tmp_path, monkeypatch):
    """After bootstrap, idx_runs_started_at must exist on the runs table."""
    monkeypatch.delenv("NOVAFABRIC_API_WORKERS", raising=False)
    s = SQLiteMetadataStore(db_path=tmp_path / "idx.db")
    s.bootstrap()

    conn = sqlite3.connect(tmp_path / "idx.db")
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='runs'"
        ).fetchall()
        index_names = {r[0] for r in rows}
    finally:
        conn.close()

    assert "idx_runs_started_at" in index_names


def test_ensure_indexes_creates_runs_status(tmp_path, monkeypatch):
    """After bootstrap, idx_runs_status must exist on the runs table."""
    monkeypatch.delenv("NOVAFABRIC_API_WORKERS", raising=False)
    s = SQLiteMetadataStore(db_path=tmp_path / "idx2.db")
    s.bootstrap()

    conn = sqlite3.connect(tmp_path / "idx2.db")
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='runs'"
        ).fetchall()
        index_names = {r[0] for r in rows}
    finally:
        conn.close()

    assert "idx_runs_status" in index_names


def test_ensure_indexes_creates_capsules_run_id(tmp_path, monkeypatch):
    """After bootstrap, idx_capsules_run_id must exist on the capsules table."""
    monkeypatch.delenv("NOVAFABRIC_API_WORKERS", raising=False)
    s = SQLiteMetadataStore(db_path=tmp_path / "idx3.db")
    s.bootstrap()

    conn = sqlite3.connect(tmp_path / "idx3.db")
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='capsules'"
        ).fetchall()
        index_names = {r[0] for r in rows}
    finally:
        conn.close()

    assert "idx_capsules_run_id" in index_names


def test_bootstrap_idempotent_schema_and_indexes(tmp_path, monkeypatch):
    """Calling bootstrap() twice on the same DB must not raise any error."""
    monkeypatch.delenv("NOVAFABRIC_API_WORKERS", raising=False)
    s = SQLiteMetadataStore(db_path=tmp_path / "twice.db")
    s.bootstrap()
    s.bootstrap()  # second call — should be a no-op for all CREATE IF NOT EXISTS


# ---------------------------------------------------------------------------
# DuckDB evidence fabric accumulator — from_ref/to_ref indexes
# ---------------------------------------------------------------------------


def test_duckdb_accumulator_ensure_indexes_idempotent(tmp_path):
    """DuckDB accumulator: _ensure_indexes() must be idempotent (call twice, no error)."""
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    from novafabric.evidence_fabric.duckdb_accumulator import DuckDBAccumulator

    db = tmp_path / "ef.duckdb"
    acc = DuckDBAccumulator(db_path=db)
    # First call is inside __init__ via _ensure_schema; calling again must not error
    acc._ensure_indexes()
    acc._ensure_indexes()


def test_duckdb_accumulator_lineage_indexes_exist(tmp_path):
    """DuckDB accumulator: idx_lineage_from and idx_lineage_to must exist after init."""
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    from novafabric.evidence_fabric.duckdb_accumulator import DuckDBAccumulator

    db = tmp_path / "ef2.duckdb"
    acc = DuckDBAccumulator(db_path=db)

    rows = acc._conn.execute(
        "SELECT index_name FROM duckdb_indexes WHERE table_name = 'lineage_edges'"
    ).fetchall()
    index_names = {r[0] for r in rows}

    assert "idx_lineage_from" in index_names, f"Expected idx_lineage_from in {index_names}"
    assert "idx_lineage_to" in index_names, f"Expected idx_lineage_to in {index_names}"
