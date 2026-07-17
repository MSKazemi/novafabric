"""NewRunTracker — watermark-based new-run detection for the SSE feed.

Regression tests for the v0.61 audit finding: the stats-refresh loop used to
load up to 10,000 run rows every tick and set-diff the full run_id set, so a
run created past the 10,000th indexed row was never broadcast on the SSE bus.
The tracker must detect new runs with cost bounded by the number of NEW rows
per poll, not the total index size.
"""

from __future__ import annotations

import sqlite3

from novafabric.registry.runs_cache import ensure_runs_cache, upsert_run
from novafabric.serve.new_run_tracker import NewRunTracker


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_runs_cache(conn)
    return conn


def _add(conn: sqlite3.Connection, run_id: str, created_at: str | None) -> None:
    upsert_run(
        conn,
        {
            "run_id": run_id,
            "status": "success",
            "created_at": created_at,
            "command": ["echo", "hi"],
        },
    )


def test_first_poll_establishes_baseline_and_returns_nothing() -> None:
    conn = _conn()
    _add(conn, "r1", "2026-07-16T10:00:00Z")
    _add(conn, "r2", "2026-07-16T10:00:01Z")
    tracker = NewRunTracker()
    assert tracker.poll(conn) == []


def test_detects_single_new_run() -> None:
    conn = _conn()
    _add(conn, "r1", "2026-07-16T10:00:00Z")
    tracker = NewRunTracker()
    tracker.poll(conn)
    _add(conn, "r2", "2026-07-16T10:00:05Z")
    new = tracker.poll(conn)
    assert [r["run_id"] for r in new] == ["r2"]
    # Not re-published on the next tick.
    assert tracker.poll(conn) == []


def test_detects_new_run_beyond_10k_existing_rows() -> None:
    """THE regression: detection must not break past 10,000 indexed runs."""
    conn = _conn()
    for i in range(10_050):
        _add(conn, f"old-{i:05d}", f"2026-07-15T00:00:00.{i:06d}Z")
    tracker = NewRunTracker()
    tracker.poll(conn)
    _add(conn, "fresh", "2026-07-16T12:00:00Z")
    new = tracker.poll(conn)
    assert [r["run_id"] for r in new] == ["fresh"]


def test_tied_timestamps_are_not_republished() -> None:
    conn = _conn()
    ts = "2026-07-16T10:00:00Z"
    _add(conn, "a", ts)
    _add(conn, "b", ts)
    tracker = NewRunTracker()
    tracker.poll(conn)
    _add(conn, "c", ts)  # same timestamp as the watermark
    new = tracker.poll(conn)
    assert [r["run_id"] for r in new] == ["c"]
    assert tracker.poll(conn) == []


def test_multiple_new_runs_returned_oldest_first() -> None:
    conn = _conn()
    _add(conn, "r1", "2026-07-16T10:00:00Z")
    tracker = NewRunTracker()
    tracker.poll(conn)
    _add(conn, "r3", "2026-07-16T10:00:03Z")
    _add(conn, "r2", "2026-07-16T10:00:02Z")
    new = tracker.poll(conn)
    assert [r["run_id"] for r in new] == ["r2", "r3"]


def test_runs_appearing_after_empty_baseline_are_published() -> None:
    conn = _conn()
    tracker = NewRunTracker()
    assert tracker.poll(conn) == []  # empty index at startup
    _add(conn, "first", "2026-07-16T10:00:00Z")
    new = tracker.poll(conn)
    assert [r["run_id"] for r in new] == ["first"]
