"""Tests for B-2 KG entity-queue REST API endpoints.

Covers:
  GET  /api/kg/entity-queue          — list pending ReviewItems
  GET  /api/kg/entity-queue/stats    — pending/approved/rejected counts
  POST /api/kg/entity-queue/{id}/approve
  POST /api/kg/entity-queue/{id}/reject
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

fastapi_installed = pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-entity-queue-0001"
TOKEN_Q = f"token={VALID_TOKEN}"
H = {"host": "127.0.0.1:4321"}


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    base = tmp_path / "runs"
    base.mkdir()
    return base


@pytest.fixture
def queue_db(tmp_path: Path) -> Path:
    return tmp_path / ".nova" / "kg" / "review_queue.db"


@pytest.fixture
def client(
    capsule_dir: Path, tmp_path: Path, queue_db: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    # Point the serve app at our tmp queue db
    monkeypatch.setenv("NOVA_KG_QUEUE_DB", str(queue_db))
    app = create_app(
        capsule_dir=capsule_dir,
        token=VALID_TOKEN,
        db_path=tmp_path / "registry.db",
        static_dir=None,
    )
    with TestClient(app) as c:
        yield c


def _pre_enqueue(queue_db: Path, aliases: list[str]) -> list[str]:
    """Helper: enqueue items directly into SQLite before hitting API."""
    from novafabric.kg.review_queue import HumanReviewQueueWriter, ReviewItem

    q = HumanReviewQueueWriter(db_path=queue_db)
    ids: list[str] = []
    for alias in aliases:
        item = ReviewItem(alias=alias, entity_type="model", confidence=0.3)
        q.enqueue(item)
        ids.append(item.item_id)
    q.close()
    return ids


# ---------------------------------------------------------------------------
# GET /api/kg/entity-queue
# ---------------------------------------------------------------------------


def test_entity_queue_list_empty(client: TestClient) -> None:
    """GET /api/kg/entity-queue returns ok=True and empty items list."""
    r = client.get(f"/api/kg/entity-queue?{TOKEN_Q}", headers=H)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["count"] == 0
    assert data["items"] == []


def test_entity_queue_list_returns_pending_items(
    client: TestClient, queue_db: Path
) -> None:
    """GET /api/kg/entity-queue returns enqueued items."""
    _pre_enqueue(queue_db, ["mystery-model-a", "mystery-model-b"])
    r = client.get(f"/api/kg/entity-queue?{TOKEN_Q}", headers=H)
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 2
    aliases = {i["alias"] for i in data["items"]}
    assert aliases == {"mystery-model-a", "mystery-model-b"}


def test_entity_queue_list_requires_auth(client: TestClient) -> None:
    """GET /api/kg/entity-queue rejects requests without a valid token."""
    r = client.get("/api/kg/entity-queue", headers=H)
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# GET /api/kg/entity-queue/stats
# ---------------------------------------------------------------------------


def test_entity_queue_stats_empty(client: TestClient) -> None:
    """GET /api/kg/entity-queue/stats returns all-zero counts."""
    r = client.get(f"/api/kg/entity-queue/stats?{TOKEN_Q}", headers=H)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["pending"] == 0
    assert data["approved"] == 0
    assert data["rejected"] == 0


def test_entity_queue_stats_after_enqueue(
    client: TestClient, queue_db: Path
) -> None:
    """Stats reflect enqueued items."""
    _pre_enqueue(queue_db, ["stat-model"])
    r = client.get(f"/api/kg/entity-queue/stats?{TOKEN_Q}", headers=H)
    assert r.status_code == 200
    data = r.json()
    assert data["pending"] == 1


# ---------------------------------------------------------------------------
# POST /api/kg/entity-queue/{item_id}/approve
# ---------------------------------------------------------------------------


def test_entity_queue_approve(client: TestClient, queue_db: Path) -> None:
    """POST /approve sets status to approved."""
    ids = _pre_enqueue(queue_db, ["approve-model"])
    item_id = ids[0]

    r = client.post(
        f"/api/kg/entity-queue/{item_id}/approve?{TOKEN_Q}",
        headers=H,
        json={"canonical": "gpt-4o", "resolved_by": "test-reviewer"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["item_id"] == item_id
    assert data["canonical"] == "gpt-4o"

    # Verify via stats
    r2 = client.get(f"/api/kg/entity-queue/stats?{TOKEN_Q}", headers=H)
    stats = r2.json()
    assert stats["approved"] == 1
    assert stats["pending"] == 0


def test_entity_queue_approve_missing_canonical(
    client: TestClient, queue_db: Path
) -> None:
    """POST /approve without canonical returns 422."""
    ids = _pre_enqueue(queue_db, ["bad-approve"])
    r = client.post(
        f"/api/kg/entity-queue/{ids[0]}/approve?{TOKEN_Q}",
        headers=H,
        json={"resolved_by": "tester"},
    )
    assert r.status_code == 422


def test_entity_queue_approve_requires_auth(client: TestClient) -> None:
    """POST /approve rejects unauthenticated requests."""
    r = client.post(
        "/api/kg/entity-queue/some-id/approve",
        headers=H,
        json={"canonical": "x"},
    )
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /api/kg/entity-queue/{item_id}/reject
# ---------------------------------------------------------------------------


def test_entity_queue_reject(client: TestClient, queue_db: Path) -> None:
    """POST /reject sets status to rejected."""
    ids = _pre_enqueue(queue_db, ["reject-model"])
    item_id = ids[0]

    r = client.post(
        f"/api/kg/entity-queue/{item_id}/reject?{TOKEN_Q}",
        headers=H,
        json={"resolved_by": "reviewer"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["status"] == "rejected"

    # Verify via stats
    r2 = client.get(f"/api/kg/entity-queue/stats?{TOKEN_Q}", headers=H)
    stats = r2.json()
    assert stats["rejected"] == 1
    assert stats["pending"] == 0


def test_entity_queue_reject_requires_auth(client: TestClient) -> None:
    """POST /reject rejects unauthenticated requests."""
    r = client.post(
        "/api/kg/entity-queue/some-id/reject",
        headers=H,
        json={},
    )
    assert r.status_code in (401, 403)
