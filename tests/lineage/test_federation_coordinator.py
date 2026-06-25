"""Tests for federation/coordinator.py (C-4) and federation/summary.py (C-5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from novafabric.lineage._types import LineageEdge
from novafabric.lineage.backends.sqlite import SqliteLineageStore
from novafabric.lineage.federation.coordinator import (
    _dedup_rows,
    create_coordinator_app,
)
from novafabric.lineage.federation.summary import SiteSummaryEntry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(tmp_path: Path, tag: str) -> SqliteLineageStore:
    """Return a seeded SqliteLineageStore with a unique set of nodes."""
    store = SqliteLineageStore(db_path=tmp_path / f"lin_{tag}.db", site_id=tag)
    store.insert(
        LineageEdge(
            edge_type="contains",
            source={"kind": "run", "run_id": f"run-{tag}-A"},
            target={"kind": "run", "run_id": f"run-{tag}-B"},
            confidence="high",
            capsule_run_id=f"cap-{tag}",
        )
    )
    return store


def _shard_response_for(store: SqliteLineageStore, site_id: str) -> dict:
    """Simulate a shard response by directly calling the store."""
    rows = store.provenance(run_id=f"run-{site_id}-A", depth=5)
    node_rows = [
        {"node_id": r["node_id"], "kind": r["kind"], "ref": r["ref"]} for r in rows
    ]
    return {"site_id": site_id, "rows": node_rows, "partial": False}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def coordinator_client() -> TestClient:
    app = create_coordinator_app(site_id="coordinator-test")
    return TestClient(app)


# ---------------------------------------------------------------------------
# C-4: coordinator fan-out tests (using respx-style mock via httpx)
# ---------------------------------------------------------------------------


def test_federation_query_merges_rows(tmp_path: Path) -> None:
    """Coordinator merges rows from 3 shards and deduplicates shared nodes."""
    store_a = _make_store(tmp_path, "site-a")
    store_b = _make_store(tmp_path, "site-b")
    store_c = _make_store(tmp_path, "site-c")

    # Insert a shared node (same run_id → same node_id) in all three stores
    shared_edge = LineageEdge(
        edge_type="spawned",
        source={"kind": "run", "run_id": "shared-run"},
        target={"kind": "run", "run_id": "shared-child"},
        confidence="high",
        capsule_run_id="shared-cap",
    )
    for s in (store_a, store_b, store_c):
        s.insert(shared_edge)

    resp_a = _shard_response_for(store_a, "site-a")
    resp_b = _shard_response_for(store_b, "site-b")
    resp_c = _shard_response_for(store_c, "site-c")

    # Manually add shared-child row to all three (provenance of run-site-x-A won't include it)
    shared_row = {"node_id": "SHARED_NODE_ID", "kind": "run", "ref": "shared-child"}
    resp_a["rows"].append(shared_row)
    resp_b["rows"].append(shared_row)
    resp_c["rows"].append(shared_row)

    site_responses = {
        "http://site-a:8000": resp_a,
        "http://site-b:8000": resp_b,
        "http://site-c:8000": resp_c,
    }

    async def _mock_post(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        base = url.rsplit("/lineage/shard-local-query", 1)[0]
        data = site_responses.get(base, {"site_id": "unknown", "rows": [], "partial": False})
        return httpx.Response(200, json=data)

    with patch.object(httpx.AsyncClient, "post", new=_mock_post):
        app = create_coordinator_app(site_id="coordinator")
        client = TestClient(app)
        resp = client.post(
            "/federation/query",
            json={
                "query_type": "provenance",
                "root_run_id": "run-site-a-A",
                "max_depth": 5,
                "sites": ["http://site-a:8000", "http://site-b:8000", "http://site-c:8000"],
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["site_count"] == 3
    assert body["dedup_count"] >= 1, "Shared node should have been deduped"
    assert isinstance(body["rows"], list)
    assert body["partial"] is False


def test_federation_query_partial_on_offline_site(tmp_path: Path) -> None:
    """Coordinator marks partial=True when any site returns 5xx."""
    store_a = _make_store(tmp_path, "site-a")
    resp_a = _shard_response_for(store_a, "site-a")

    call_count = 0

    async def _mock_post_with_failure(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if "site-b" in url:
            return httpx.Response(500, json={"detail": "Internal Server Error"})
        return httpx.Response(200, json=resp_a)

    with patch.object(httpx.AsyncClient, "post", new=_mock_post_with_failure):
        app = create_coordinator_app(site_id="coordinator")
        client = TestClient(app)
        resp = client.post(
            "/federation/query",
            json={
                "query_type": "provenance",
                "root_run_id": "run-site-a-A",
                "max_depth": 5,
                "sites": ["http://site-a:8000", "http://site-b:8000"],
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["partial"] is True, "partial must be True when a site returns 500"


def test_federation_query_empty_sites() -> None:
    """Empty site list returns empty rows with zero dedup."""
    app = create_coordinator_app(site_id="coordinator")
    client = TestClient(app)
    resp = client.post(
        "/federation/query",
        json={
            "query_type": "provenance",
            "root_run_id": "any-run",
            "max_depth": 5,
            "sites": [],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["rows"] == []
    assert body["site_count"] == 0
    assert body["dedup_count"] == 0


def test_dedup_rows_removes_duplicate_node_ids() -> None:
    rows = [
        {"node_id": "abc", "kind": "run", "ref": "r1"},
        {"node_id": "def", "kind": "run", "ref": "r2"},
        {"node_id": "abc", "kind": "run", "ref": "r1"},  # duplicate
    ]
    result = _dedup_rows(rows)
    assert len(result) == 2
    assert result[0]["node_id"] == "abc"
    assert result[1]["node_id"] == "def"


def test_dedup_count_in_response(tmp_path: Path) -> None:
    """dedup_count must equal the number of rows removed by deduplication."""
    dup_row = {"node_id": "DUP_ID", "kind": "run", "ref": "dup-run"}
    resp_a = {"site_id": "site-a", "rows": [dup_row], "partial": False}
    resp_b = {"site_id": "site-b", "rows": [dup_row], "partial": False}

    async def _mock_dup(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        if "site-a" in url:
            return httpx.Response(200, json=resp_a)
        return httpx.Response(200, json=resp_b)

    with patch.object(httpx.AsyncClient, "post", new=_mock_dup):
        app = create_coordinator_app(site_id="coordinator")
        client = TestClient(app)
        resp = client.post(
            "/federation/query",
            json={
                "query_type": "provenance",
                "root_run_id": "any",
                "max_depth": 5,
                "sites": ["http://site-a:8000", "http://site-b:8000"],
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["dedup_count"] == 1
    assert len(body["rows"]) == 1


# ---------------------------------------------------------------------------
# C-5: summary endpoint tests
# ---------------------------------------------------------------------------


def test_summary_returns_200() -> None:
    app = create_coordinator_app(site_id="coordinator")
    client = TestClient(app)
    resp = client.get("/federation/summary")
    assert resp.status_code == 200


def test_summary_response_shape() -> None:
    app = create_coordinator_app(site_id="coordinator")
    client = TestClient(app)
    resp = client.get("/federation/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert "entries" in body
    assert "source" in body
    assert body["source"] == "summary"
    assert "refreshed_at" in body
    assert isinstance(body["entries"], list)


def test_summary_with_fetch_fn() -> None:
    """Summary should serve data from the provided fetch_fn."""
    entries = [
        SiteSummaryEntry(
            source_site_id="site-alpha",
            target_site_id="site-beta",
            edge_type="delegated_to",
            count=42,
        )
    ]
    app = create_coordinator_app(
        site_id="coordinator",
        summary_fetch_fn=lambda: entries,
        summary_refresh_interval=0.0,
    )
    client = TestClient(app)
    # Trigger startup to populate the cache
    with client:
        resp = client.get("/federation/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["entries"]) == 1
    assert body["entries"][0]["source_site_id"] == "site-alpha"
    assert body["entries"][0]["count"] == 42
