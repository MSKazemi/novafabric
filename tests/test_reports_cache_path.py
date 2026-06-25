"""D1c: reports should read the runs_cache index instead of scanning disk.

These tests assert output *parity* — the cache-backed path must return the
same columns and rows as the disk-scan path for the same data — and that the
report falls back to the disk scan when the cache is empty/unavailable.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from novafabric.registry.runs_cache import build_runs_index, ensure_runs_cache
from novafabric.serve.reports import report_cost_burn, report_run_history


def _make_capsule(base: Path, run_id: str, status: str, created_at: str,
                  *, model: int = 2, tool: int = 1,
                  command: list[str] | None = None) -> None:
    d = base / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "capsule.yaml").write_text(
        yaml.dump({
            "run_id": run_id,
            "status": status,
            "created_at": created_at,
            "finished_at": created_at,
            "duration_ms": 100,
            "exit_code": 0 if status == "ok" else 1,
            "model_call_count": model,
            "tool_call_count": tool,
            "mutating_tool_count": 0,
            "command": command or ["python", "agent.py"],
            "novafabric_version": "0.47.0",
        })
    )


@pytest.fixture()
def capsules(tmp_path: Path) -> Path:
    base = tmp_path / "capsules"
    _make_capsule(base, "run_a", "ok", "2026-05-01T10:00:00Z", command=["agentX"])
    _make_capsule(base, "run_b", "error", "2026-05-02T10:00:00Z", command=["agentX"])
    _make_capsule(base, "run_c", "ok", "2026-05-03T10:00:00Z", command=["agentY"])
    return base


def _populate_cache(db_path: Path, capsule_dir: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ensure_runs_cache(conn)
    build_runs_index(capsule_dir, conn, incremental=False)
    conn.close()


def _norm(rows: list[dict]) -> set[tuple]:
    """Order-independent comparison key over run-history rows."""
    return {tuple(sorted(r.items())) for r in rows}


def test_run_history_cache_matches_disk(capsules: Path, tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    _populate_cache(db, capsules)

    cols_disk, rows_disk = report_run_history(capsules)
    cols_cache, rows_cache = report_run_history(capsules, db_path=db)

    assert cols_cache == cols_disk
    assert _norm(rows_cache) == _norm(rows_disk)


def test_run_history_cache_status_filter_matches(capsules: Path, tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    _populate_cache(db, capsules)

    _, rows_disk = report_run_history(capsules, status="ok")
    _, rows_cache = report_run_history(capsules, status="ok", db_path=db)

    assert _norm(rows_cache) == _norm(rows_disk)
    assert all(r["status"] == "ok" for r in rows_cache)


def test_run_history_cache_date_filter_matches(capsules: Path, tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    _populate_cache(db, capsules)

    kw = {"from_ts": "2026-05-02T00:00:00Z", "to_ts": "2026-05-03T23:59:59Z"}
    _, rows_disk = report_run_history(capsules, **kw)
    _, rows_cache = report_run_history(capsules, db_path=db, **kw)

    assert _norm(rows_cache) == _norm(rows_disk)


def test_cost_burn_cache_matches_disk(capsules: Path, tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    _populate_cache(db, capsules)

    cols_disk, rows_disk = report_cost_burn(capsules)
    cols_cache, rows_cache = report_cost_burn(capsules, db_path=db)

    assert cols_cache == cols_disk
    assert rows_cache == rows_disk  # cost_burn already sorts deterministically


def test_reports_fall_back_when_cache_empty(capsules: Path, tmp_path: Path) -> None:
    # db exists but runs_cache is empty -> must scan disk, not report zero runs.
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db))
    ensure_runs_cache(conn)
    conn.close()

    _, rows_cache = report_run_history(capsules, db_path=db)
    _, rows_disk = report_run_history(capsules)
    assert _norm(rows_cache) == _norm(rows_disk)
    assert len(rows_cache) == 3


def test_reports_fall_back_when_db_missing(capsules: Path, tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.db"
    _, rows = report_run_history(capsules, db_path=missing)
    # ensure_runs_cache creates the file but it is empty -> disk fallback.
    assert len(rows) == 3
