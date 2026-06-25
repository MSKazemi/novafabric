"""Tests for Scale-S1: runs_cache indexed capsule summaries."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import yaml

from novafabric.registry.runs_cache import (
    build_runs_index,
    count_cached_runs,
    ensure_runs_cache,
    query_runs,
    upsert_run,
)


def _make_capsule(base: Path, run_id: str, status: str, created_at: str) -> Path:
    """Create a minimal capsule directory with capsule.yaml."""
    d = base / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "capsule.yaml").write_text(
        yaml.dump({
            "run_id": run_id,
            "status": status,
            "created_at": created_at,
            "finished_at": created_at,
            "duration_ms": 100,
            "exit_code": 0,
            "model_call_count": 2,
            "tool_call_count": 1,
            "mutating_tool_count": 0,
            "command": ["python", "agent.py"],
            "novafabric_version": "0.31.0",
        })
    )
    return d


def _open_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


class TestEnsureRunsCache:
    def test_creates_table(self):
        conn = _open_conn()
        ensure_runs_cache(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "runs_cache" in tables

    def test_idempotent(self):
        conn = _open_conn()
        ensure_runs_cache(conn)
        ensure_runs_cache(conn)  # must not raise
        assert count_cached_runs(conn) == 0

    def test_composite_status_created_at_index_exists(self):
        # D1b: a composite (status, created_at) index must back the common
        # "filter by status, newest-first" dashboard query.
        conn = _open_conn()
        ensure_runs_cache(conn)
        idx = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_runs_cache_status_created_at" in idx

    def test_composite_index_is_used_by_planner(self):
        conn = _open_conn()
        ensure_runs_cache(conn)
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM runs_cache "
            "WHERE status = ? ORDER BY created_at DESC",
            ("ok",),
        ).fetchall()
        plan_text = " ".join(str(row[-1]) for row in plan)
        assert "idx_runs_cache_status_created_at" in plan_text


class TestUpsertRun:
    def test_inserts_and_reads_back(self):
        conn = _open_conn()
        ensure_runs_cache(conn)
        summary = {
            "run_id": "run_001",
            "status": "success",
            "created_at": "2026-01-01T00:00:00Z",
            "command": ["nova", "capture"],
            "model_call_count": 3,
        }
        upsert_run(conn, summary)
        conn.commit()
        page, total = query_runs(conn)
        assert total == 1
        assert page[0]["run_id"] == "run_001"
        assert page[0]["command"] == ["nova", "capture"]

    def test_replace_on_duplicate_run_id(self):
        conn = _open_conn()
        ensure_runs_cache(conn)
        upsert_run(conn, {"run_id": "run_001", "status": "running"})
        upsert_run(conn, {"run_id": "run_001", "status": "success"})
        conn.commit()
        page, total = query_runs(conn)
        assert total == 1
        assert page[0]["status"] == "success"


class TestBuildRunsIndex:
    def test_indexes_all_capsules(self, tmp_path):
        _make_capsule(tmp_path, "run_2026_001", "success", "2026-01-01T10:00:00Z")
        _make_capsule(tmp_path, "run_2026_002", "failed", "2026-01-02T10:00:00Z")
        conn = _open_conn()
        ensure_runs_cache(conn)
        inserted = build_runs_index(tmp_path, conn)
        assert inserted == 2
        assert count_cached_runs(conn) == 2

    def test_incremental_skips_existing(self, tmp_path):
        _make_capsule(tmp_path, "run_2026_001", "success", "2026-01-01T10:00:00Z")
        conn = _open_conn()
        ensure_runs_cache(conn)
        build_runs_index(tmp_path, conn)
        # Add second capsule
        _make_capsule(tmp_path, "run_2026_002", "success", "2026-01-02T10:00:00Z")
        inserted = build_runs_index(tmp_path, conn, incremental=True)
        assert inserted == 1
        assert count_cached_runs(conn) == 2

    def test_non_incremental_reindexes_all(self, tmp_path):
        _make_capsule(tmp_path, "run_2026_001", "success", "2026-01-01T10:00:00Z")
        conn = _open_conn()
        ensure_runs_cache(conn)
        build_runs_index(tmp_path, conn)
        inserted = build_runs_index(tmp_path, conn, incremental=False)
        assert inserted == 1  # REPLACE counts as 1 insert
        assert count_cached_runs(conn) == 1

    def test_empty_capsule_dir(self, tmp_path):
        conn = _open_conn()
        ensure_runs_cache(conn)
        inserted = build_runs_index(tmp_path, conn)
        assert inserted == 0

    def test_skips_dirs_without_capsule_yaml(self, tmp_path):
        (tmp_path / "not-a-capsule").mkdir()
        conn = _open_conn()
        ensure_runs_cache(conn)
        inserted = build_runs_index(tmp_path, conn)
        assert inserted == 0


class TestQueryRuns:
    def _populated(self):
        conn = _open_conn()
        ensure_runs_cache(conn)
        for i in range(5):
            upsert_run(conn, {
                "run_id": f"run_{i:03d}",
                "status": "success" if i % 2 == 0 else "failed",
                "created_at": f"2026-01-0{i+1}T00:00:00Z",
                "command": ["python", f"agent_{i}.py"],
            })
        conn.commit()
        return conn

    def test_returns_newest_first(self):
        conn = self._populated()
        page, total = query_runs(conn, limit=10)
        assert total == 5
        created_ats = [r["created_at"] for r in page]
        assert created_ats == sorted(created_ats, reverse=True)

    def test_limit_offset(self):
        conn = self._populated()
        page1, total = query_runs(conn, limit=2, offset=0)
        page2, _ = query_runs(conn, limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        assert {r["run_id"] for r in page1}.isdisjoint({r["run_id"] for r in page2})

    def test_status_filter(self):
        conn = self._populated()
        page, total = query_runs(conn, status="failed")
        assert all(r["status"] == "failed" for r in page)
        assert total == 2  # runs 1, 3

    def test_since_filter(self):
        conn = self._populated()
        page, total = query_runs(conn, since="2026-01-04T00:00:00Z")
        assert all(r["created_at"] >= "2026-01-04T00:00:00Z" for r in page)

    def test_free_text_search(self):
        conn = self._populated()
        page, total = query_runs(conn, q="agent_2")
        assert total == 1
        assert page[0]["run_id"] == "run_002"

    def test_empty_cache_returns_zero(self):
        conn = _open_conn()
        ensure_runs_cache(conn)
        page, total = query_runs(conn)
        assert total == 0
        assert page == []

    def test_command_deserialized_as_list(self):
        conn = _open_conn()
        ensure_runs_cache(conn)
        upsert_run(conn, {"run_id": "r1", "command": ["a", "b", "c"]})
        conn.commit()
        page, _ = query_runs(conn)
        assert page[0]["command"] == ["a", "b", "c"]

    def test_null_command_returns_empty_list(self):
        conn = _open_conn()
        ensure_runs_cache(conn)
        upsert_run(conn, {"run_id": "r1"})
        conn.commit()
        page, _ = query_runs(conn)
        assert page[0]["command"] == []
