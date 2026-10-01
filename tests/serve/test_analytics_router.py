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


# ---------------------------------------------------------------------------
# ADR-0234 D2 — the aggregate verdict
# ---------------------------------------------------------------------------


def _get(c: TestClient, **params: str) -> dict:
    r = c.get("/api/analytics/summary", params={"token": TOKEN, **params}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_complete_index_is_computable_and_flags_small_samples(client: TestClient) -> None:
    data = _get(client)
    agg = data["aggregate"]
    assert agg["computable"] is True
    assert agg["value"] == data["totals"]
    assert agg["notes"]["source"] == "runs_cache"
    # Three runs over two days: every bucket's p95 rests on fewer than 20 runs.
    assert agg["notes"]["small_sample_buckets"] == ["2026-07-14", "2026-07-15"]
    assert [b["duration_samples"] for b in data["buckets"]] == [2, 1]


@pytest.mark.parametrize(
    ("params", "named"),
    [({"f": "status:error"}, "filter"), ({"status": "error"}, "status"), ({"q": "abc"}, "search")],
)
def test_an_unpushable_view_refuses_without_numbers(
    client: TestClient, params: dict, named: str
) -> None:
    data = _get(client, **params)
    agg = data["aggregate"]
    assert agg["computable"] is False
    assert agg["condition"] == "unpushable_filter"
    assert "value" not in agg
    assert named in agg["reason"] and agg["remedy"]
    assert data["totals"] is None and data["buckets"] == []


def test_the_all_status_chip_is_not_a_filter(client: TestClient) -> None:
    assert _get(client, status="all")["aggregate"]["computable"] is True


def test_a_partial_index_refuses_as_truncated(tmp_path: Path) -> None:
    base = tmp_path / "capsules"
    base.mkdir()
    for i in range(5):  # five capsules on disk, three in the index
        (base / f"cap{i}").mkdir()
        (base / f"cap{i}" / "capsule.yaml").write_text("run_id: x\n")
    db = tmp_path / "registry.db"
    _seed(db)
    c = TestClient(create_app(token=TOKEN, capsule_dir=base, db_path=db, static_dir=None))
    data = _get(c)
    agg = data["aggregate"]
    assert agg["condition"] == "truncated_source"
    assert agg["notes"] == {"indexed": 3, "on_disk": 5}
    assert data["totals"] is None


def test_a_missing_index_refuses_only_when_capsules_exist(tmp_path: Path) -> None:
    from fastapi import FastAPI

    from novafabric.serve.routers.analytics import build_analytics_router

    base = tmp_path / "capsules"
    base.mkdir()

    def _ok() -> str:
        return "ok"

    app = FastAPI()
    app.include_router(
        build_analytics_router(_ok, db_path=tmp_path / "absent.db", capsule_dir=base)
    )
    c = TestClient(app)
    empty = _get(c)
    assert empty["aggregate"]["computable"] is True
    assert empty["totals"]["run_count"] == 0

    (base / "cap").mkdir()
    (base / "cap" / "capsule.yaml").write_text("run_id: x\n")
    data = _get(c)
    assert data["aggregate"]["condition"] == "source_unavailable"
    assert data["totals"] is None
