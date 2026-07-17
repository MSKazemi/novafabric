"""Analytics summary router (dashboard analytics slice, 2026-07-16 audit wave 5).

`GET /api/analytics/summary` returns time-bucketed aggregates computed from
the runs_cache index (no capsule scans): run volume, failure counts,
duration percentiles, and model/tool-call volume per day. Serve-side
pre-aggregation — the dashboard charts consume buckets, never raw rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novafabric.registry.runs_cache import ensure_runs_cache, upsert_run
from novafabric.registry.store import get_connection, init_schema
from novafabric.serve.app import create_app

TOKEN = "testtoken"
H = {"host": "127.0.0.1:4321"}


def _seed(db: Path) -> None:
    conn = get_connection(db)
    init_schema(conn)
    ensure_runs_cache(conn)
    rows = [
        # day 1: two runs (one failed), durations 100/300
        ("a1", "success", "2026-07-14T09:00:00Z", 100.0, 3, 1),
        ("a2", "error", "2026-07-14T10:00:00Z", 300.0, 1, 0),
        # day 2: one run, duration 200
        ("b1", "success", "2026-07-15T09:30:00Z", 200.0, 5, 2),
    ]
    for run_id, status, ts, dur, mc, tc in rows:
        upsert_run(
            conn,
            {
                "run_id": run_id,
                "status": status,
                "created_at": ts,
                "duration_ms": dur,
                "model_call_count": mc,
                "tool_call_count": tc,
                "command": [],
            },
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    base = tmp_path / "capsules"
    base.mkdir()
    db = tmp_path / "registry.db"
    _seed(db)
    app = create_app(token=TOKEN, capsule_dir=base, db_path=db, static_dir=None)
    return TestClient(app)


def test_requires_token(client: TestClient) -> None:
    r = client.get("/api/analytics/summary", headers=H)
    assert r.status_code == 401


def test_daily_buckets_aggregate_runs(client: TestClient) -> None:
    r = client.get(
        "/api/analytics/summary",
        params={"token": TOKEN, "since": "2026-07-01", "until": "2026-07-31"},
        headers=H,
    )
    assert r.status_code == 200
    data = r.json()
    buckets = {b["bucket"]: b for b in data["buckets"]}
    d1 = buckets["2026-07-14"]
    assert d1["run_count"] == 2
    assert d1["failed_count"] == 1
    assert d1["model_call_count"] == 4
    assert d1["tool_call_count"] == 1
    assert d1["duration_ms_p50"] == pytest.approx(200.0)
    assert d1["duration_ms_max"] == pytest.approx(300.0)
    d2 = buckets["2026-07-15"]
    assert d2["run_count"] == 1
    assert d2["failed_count"] == 0


def test_totals_block(client: TestClient) -> None:
    r = client.get(
        "/api/analytics/summary",
        params={"token": TOKEN, "since": "2026-07-01", "until": "2026-07-31"},
        headers=H,
    )
    totals = r.json()["totals"]
    assert totals["run_count"] == 3
    assert totals["failed_count"] == 1
    assert totals["model_call_count"] == 9


def test_window_filter_excludes_outside_runs(client: TestClient) -> None:
    r = client.get(
        "/api/analytics/summary",
        params={"token": TOKEN, "since": "2026-07-15", "until": "2026-07-31"},
        headers=H,
    )
    data = r.json()
    assert data["totals"]["run_count"] == 1
    assert [b["bucket"] for b in data["buckets"]] == ["2026-07-15"]


def test_empty_index_returns_empty_shape(tmp_path: Path) -> None:
    base = tmp_path / "capsules"
    base.mkdir()
    app = create_app(
        token=TOKEN, capsule_dir=base, db_path=tmp_path / "r.db", static_dir=None
    )
    c = TestClient(app)
    r = c.get("/api/analytics/summary", params={"token": TOKEN}, headers=H)
    assert r.status_code == 200
    data = r.json()
    assert data["buckets"] == []
    assert data["totals"]["run_count"] == 0
