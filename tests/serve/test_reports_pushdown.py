"""S2 (ADR-0199) — aggregation pushdown for the serve report builders.

Covers the new SQL aggregate functions in ``novafabric.registry.runs_cache``
(day buckets, windowed throughput, per-agent totals, whole-window totals, the
``substr(created_at, 1, 10)`` expression index) and the report builders'
cache-first paths: when the runs_cache index is populated the builders must
aggregate in SQL and never materialize row lists; when it is empty they must
fall back to the capsule filesystem scan exactly as before.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from novafabric.registry.runs_cache import (
    aggregate_runs_by_agent,
    aggregate_runs_daily,
    aggregate_runs_windowed,
    durations_by_bucket,
    ensure_runs_cache,
    runs_totals,
    upsert_run,
)
from novafabric.serve.reports import (
    COST_BURN_COLS,
    EXECUTIVE_COLS,
    THROUGHPUT_COLS,
    iter_run_history_csv,
    report_cost_burn,
    report_executive_summary,
    report_run_history_page,
    report_throughput,
)

_RUNS = [
    # run_id, status, created_at, duration_ms, exit_code, model, tool, command
    ("r1", "success", "2026-07-01T08:00:00Z", 100.0, 0, 3, 1, ["agent-a", "x"]),
    ("r2", "failure", "2026-07-01T09:00:00Z", 200.0, 1, 1, 0, ["agent-a", "y"]),
    ("r3", "ok", "2026-07-02T10:00:00Z", 300.0, 1, 2, 2, ["agent-b"]),
    ("r4", "success", "2026-07-02T11:00:00Z", None, 0, 0, 0, None),
]


def _seed(db: Path) -> None:
    con = sqlite3.connect(str(db))
    ensure_runs_cache(con)
    for run_id, status, created, dur, exit_code, mc, tc, cmd in _RUNS:
        upsert_run(
            con,
            {
                "run_id": run_id,
                "status": status,
                "created_at": created,
                "finished_at": created,
                "duration_ms": dur,
                "exit_code": exit_code,
                "model_call_count": mc,
                "tool_call_count": tc,
                "command": cmd,
            },
        )
    con.commit()
    con.close()


def _conn(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    return con


# --------------------------------------------------------------------------- #
# runs_cache aggregate functions
# --------------------------------------------------------------------------- #

def test_expression_index_created(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    _seed(db)
    con = _conn(db)
    names = {
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    con.close()
    assert "idx_runs_cache_created_day" in names


def test_aggregate_runs_daily_buckets_and_window(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    _seed(db)
    con = _conn(db)
    buckets = aggregate_runs_daily(con)
    assert [b["bucket"] for b in buckets] == ["2026-07-01", "2026-07-02"]
    d1 = buckets[0]
    # default failed predicate: status != 'success' (r2 failure counts; r3 "ok"
    # counts as failed under the analytics convention, but is in day 2)
    assert d1["run_count"] == 2
    assert d1["failed_count"] == 1
    assert d1["model_call_count"] == 4
    # window filter: only day 2
    only_d2 = aggregate_runs_daily(con, since="2026-07-02")
    assert [b["bucket"] for b in only_d2] == ["2026-07-02"]
    # until compares on the date prefix (whole day covered)
    only_d1 = aggregate_runs_daily(con, until="2026-07-01T00:00:00Z")
    assert [b["bucket"] for b in only_d1] == ["2026-07-01"]
    con.close()


def test_durations_by_bucket_ordered_and_null_free(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    _seed(db)
    con = _conn(db)
    pairs = durations_by_bucket(con)
    con.close()
    assert pairs == [
        ("2026-07-01", 100.0),
        ("2026-07-01", 200.0),
        ("2026-07-02", 300.0),
    ]


def test_aggregate_runs_windowed_success_semantics(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    _seed(db)
    con = _conn(db)
    windows = aggregate_runs_windowed(con, prefix_len=10)
    con.close()
    by_window = {w["window"]: w for w in windows}
    # report success semantics: exit_code == 0 OR status == 'ok'
    assert by_window["2026-07-01"]["successes"] == 1  # r1 only
    assert by_window["2026-07-01"]["failures"] == 1  # r2
    assert by_window["2026-07-02"]["successes"] == 2  # r3 (ok) + r4 (exit 0)
    assert by_window["2026-07-02"]["failures"] == 0


def test_aggregate_runs_by_agent_first_token_and_unknown(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    _seed(db)
    con = _conn(db)
    rows = aggregate_runs_by_agent(con)
    con.close()
    by_agent = {r["agent"]: r for r in rows}
    assert by_agent["agent-a"]["runs"] == 2
    assert by_agent["agent-a"]["model_calls"] == 4
    assert by_agent["agent-b"]["tool_calls"] == 2
    assert by_agent["(unknown)"]["runs"] == 1  # r4 has no command
    # sorted by runs DESC — the busiest agent leads
    assert rows[0]["agent"] == "agent-a"


def test_runs_totals(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    _seed(db)
    con = _conn(db)
    totals = runs_totals(con)
    con.close()
    assert totals == {
        "total_runs": 4,
        "successes": 3,
        "failures": 1,
        "model_calls": 6,
        "tool_calls": 3,
    }


# --------------------------------------------------------------------------- #
# report builders: cache path vs filesystem fallback
# --------------------------------------------------------------------------- #

def test_report_cost_burn_uses_cache(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    _seed(db)
    cols, rows = report_cost_burn(tmp_path / "no-capsules", db_path=db)
    assert cols == COST_BURN_COLS
    assert {r["agent"] for r in rows} == {"agent-a", "agent-b", "(unknown)"}


def test_report_throughput_uses_cache(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    _seed(db)
    cols, rows = report_throughput(tmp_path / "no-capsules", db_path=db)
    assert cols == THROUGHPUT_COLS
    by_window = {r["window"]: r for r in rows}
    assert by_window["2026-07-01"]["success_rate_pct"] == 50.0
    assert by_window["2026-07-02"]["success_rate_pct"] == 100.0
    assert set(rows[0]) >= set(THROUGHPUT_COLS)


def test_report_executive_summary_uses_cache(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    _seed(db)
    cols, rows = report_executive_summary(tmp_path / "no-capsules", db_path=db)
    assert cols == EXECUTIVE_COLS
    assert rows[0]["total_runs"] == 4
    assert rows[0]["successes"] == 3
    assert rows[0]["success_rate_pct"] == 75.0
    assert rows[0]["total_model_calls"] == 6


def test_run_history_page_keyset_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    _seed(db)
    caps = tmp_path / "no-capsules"
    cols, page1, after1, total = report_run_history_page(
        caps, db_path=db, limit=3
    )
    assert total == 4
    assert len(page1) == 3
    assert after1 is not None
    cols, page2, after2, _ = report_run_history_page(
        caps, db_path=db, limit=3, after=after1
    )
    assert [r["run_id"] for r in page2] == ["r1"]  # oldest run last (DESC order)
    assert after2 is None or after2 != after1
    # no duplicates, full coverage
    ids = [r["run_id"] for r in page1 + page2]
    assert sorted(ids) == ["r1", "r2", "r3", "r4"]


def test_run_history_page_agent_filter_sql(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    _seed(db)
    _, rows, _, total = report_run_history_page(
        tmp_path / "caps", db_path=db, agent="agent-b"
    )
    assert total == 1
    assert [r["run_id"] for r in rows] == ["r3"]


def test_iter_run_history_csv_single_header(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    _seed(db)
    chunks = list(
        iter_run_history_csv(tmp_path / "caps", db_path=db, page_size=2)
    )
    body = "".join(chunks)
    lines = [ln for ln in body.split("\r\n") if ln]
    assert lines[0].startswith("run_id,")
    assert sum(1 for ln in lines if ln.startswith("run_id,")) == 1  # one header
    assert len(lines) == 1 + 4  # header + all rows across pages


def test_report_builders_fall_back_when_cache_empty(tmp_path: Path) -> None:
    """An empty cache signals filesystem fallback, not zero rows."""
    empty_db = tmp_path / "empty.db"
    con = sqlite3.connect(str(empty_db))
    ensure_runs_cache(con)
    con.commit()
    con.close()
    # No capsules on disk either — every builder degrades to empty, not error.
    for cols, rows in (
        report_cost_burn(tmp_path / "caps", db_path=empty_db),
        report_throughput(tmp_path / "caps", db_path=empty_db),
        report_executive_summary(tmp_path / "caps", db_path=empty_db),
    ):
        assert isinstance(cols, list)
        assert rows == [] or rows[0].get("total_runs") == 0
