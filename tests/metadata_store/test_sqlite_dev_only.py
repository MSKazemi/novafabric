"""Tests for SQLiteMetadataStore — dev-only, single-process backend.

TDD — tests written before implementation. Run sequence:
  1. pytest tests/metadata_store/test_sqlite_dev_only.py -v  → all FAIL
  2. implement sqlite.py
  3. pytest tests/metadata_store/test_sqlite_dev_only.py -v  → all PASS
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from novafabric.metadata_store import BackendModeError  # noqa: E402
from novafabric.metadata_store.sqlite import SQLiteMetadataStore  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A bootstrapped SQLiteMetadataStore using a temp DB; workers env unset."""
    monkeypatch.delenv("NOVAFABRIC_API_WORKERS", raising=False)
    s = SQLiteMetadataStore(db_path=tmp_path / "test_meta.db")
    s.bootstrap()
    return s


# ---------------------------------------------------------------------------
# Test 1: multi-worker startup raises BackendModeError immediately
# ---------------------------------------------------------------------------


def test_multi_worker_startup_fails(tmp_path, monkeypatch):
    """Setting NOVAFABRIC_API_WORKERS > 1 must raise BackendModeError at construction."""
    monkeypatch.setenv("NOVAFABRIC_API_WORKERS", "4")
    with pytest.raises(BackendModeError) as exc_info:
        SQLiteMetadataStore(db_path=tmp_path / "should_not_create.db")
    assert "multi-worker" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 2: single-worker startup succeeds
# ---------------------------------------------------------------------------


def test_single_worker_startup_succeeds(tmp_path, monkeypatch):
    """With workers == 1 (or unset) construction succeeds and health_check returns ok."""
    monkeypatch.delenv("NOVAFABRIC_API_WORKERS", raising=False)
    s = SQLiteMetadataStore(db_path=tmp_path / "meta.db")
    s.bootstrap()
    result = s.health_check()
    assert result["status"] == "ok"
    assert result["backend"] == "sqlite"


# ---------------------------------------------------------------------------
# Test 3: begin_tenant_context is a no-op pass-through
# ---------------------------------------------------------------------------


def test_begin_tenant_context_is_noop(store):
    """begin_tenant_context() must yield the store itself (single-tenant dev no-op)."""
    tenant = uuid4()
    with store.begin_tenant_context(tenant) as ctx:
        assert ctx is store


# ---------------------------------------------------------------------------
# Test 4: register_run + lookup_run round-trip
# ---------------------------------------------------------------------------


def test_register_and_lookup_run(store):
    """A registered run must be retrievable by (run_id, tenant_id)."""
    run_id = uuid4()
    tenant_id = uuid4()
    store.register_run(
        run_id,
        tenant_id,
        event_type="run.started",
        global_run_id=str(uuid4()),
        started_at="2026-05-13T00:00:00Z",
        world_size=1,
        expected_children=0,
    )
    result = store.lookup_run(run_id, tenant_id)
    assert result is not None
    assert result["run_id"] == str(run_id)


# ---------------------------------------------------------------------------
# Test 5: query_runs pagination
# ---------------------------------------------------------------------------


def test_query_runs_pagination(store):
    """register 5 runs, query limit=3 → 3 rows + cursor; query cursor → 2 rows + None."""
    tenant_id = uuid4()
    for _ in range(5):
        store.register_run(
            uuid4(),
            tenant_id,
            event_type="run.started",
            started_at="2026-05-13T00:00:00Z",
        )

    page1, cursor1 = store.query_runs(tenant_id, limit=3)
    assert len(page1) == 3
    assert cursor1 is not None

    page2, cursor2 = store.query_runs(tenant_id, limit=3, cursor=cursor1)
    assert len(page2) == 2
    assert cursor2 is None
