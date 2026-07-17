"""Cursor (keyset) pagination on /api/runs (v0.61 audit, Wave 2).

Offset pagination is O(offset) on a growing index; the search endpoint
already uses an opaque (created_at, run_id) cursor. /api/runs now accepts
the same `cursor` parameter (additive — offset behavior unchanged) and
returns `next_cursor` for stable, index-friendly paging.
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


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    base = tmp_path / "capsules"
    base.mkdir()
    db = tmp_path / "registry.db"
    conn = get_connection(db)
    init_schema(conn)
    ensure_runs_cache(conn)
    # 5 runs, two sharing one timestamp to exercise the (ts, id) tie-break.
    stamps = [
        ("r5", "2026-07-16T10:00:05Z"),
        ("r4", "2026-07-16T10:00:04Z"),
        ("r3b", "2026-07-16T10:00:03Z"),
        ("r3a", "2026-07-16T10:00:03Z"),
        ("r1", "2026-07-16T10:00:01Z"),
    ]
    for run_id, ts in stamps:
        upsert_run(
            conn,
            {"run_id": run_id, "status": "success", "created_at": ts, "command": []},
        )
    conn.commit()
    conn.close()
    app = create_app(token=TOKEN, capsule_dir=base, db_path=db, static_dir=None)
    return TestClient(app)


def _get(client: TestClient, **params: str | int) -> dict:
    r = client.get("/api/runs", params={"token": TOKEN, **params}, headers=H)
    assert r.status_code == 200
    return r.json()


def test_first_page_returns_next_cursor(client: TestClient) -> None:
    data = _get(client, limit=2)
    assert [r["run_id"] for r in data["runs"]] == ["r5", "r4"]
    assert data["next_cursor"]


def test_cursor_walks_all_pages_without_overlap_or_gap(client: TestClient) -> None:
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        data = _get(client, limit=2, **({"cursor": cursor} if cursor else {}))
        seen.extend(r["run_id"] for r in data["runs"])
        cursor = data.get("next_cursor")
        if not cursor:
            break
    assert seen == ["r5", "r4", "r3b", "r3a", "r1"]


def test_tied_timestamps_break_on_run_id(client: TestClient) -> None:
    page1 = _get(client, limit=3)
    assert [r["run_id"] for r in page1["runs"]] == ["r5", "r4", "r3b"]
    page2 = _get(client, limit=3, cursor=page1["next_cursor"])
    assert [r["run_id"] for r in page2["runs"]] == ["r3a", "r1"]


def test_exhausted_cursor_returns_empty_and_no_next(client: TestClient) -> None:
    data = _get(client, limit=5)
    tail = _get(client, limit=5, cursor=data["next_cursor"])
    assert tail["runs"] == []
    assert tail.get("next_cursor") is None


def test_offset_mode_unchanged(client: TestClient) -> None:
    data = _get(client, limit=2, offset=2)
    assert [r["run_id"] for r in data["runs"]] == ["r3b", "r3a"]
    assert data["total"] == 5
    assert data["has_more"] is True


def test_invalid_cursor_falls_back_to_first_page(client: TestClient) -> None:
    data = _get(client, limit=2, cursor="not-a-cursor")
    assert [r["run_id"] for r in data["runs"]] == ["r5", "r4"]
