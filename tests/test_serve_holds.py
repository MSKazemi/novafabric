"""Tests for the legal-holds API routes in the experimental serve app."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novafabric.serve.app import create_app

LOCALHOST_HEADERS = {"host": "127.0.0.1:4321"}
TOKEN = "test-token-holds"


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = create_app(token=TOKEN, capsule_dir=tmp_path / ".novafabric" / "runs", db_path=None)
    return app, tmp_path


def _make_client(tmp_path: Path) -> tuple[TestClient, Path]:
    app = create_app(token=TOKEN, capsule_dir=tmp_path / ".novafabric" / "runs", db_path=None)
    return TestClient(app), tmp_path


def test_list_holds_empty(tmp_path: Path) -> None:
    tc, _ = _make_client(tmp_path)
    r = tc.get("/api/holds", params={"token": TOKEN}, headers=LOCALHOST_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["total_active"] == 0
    assert data["registries"] == []


def test_create_hold(tmp_path: Path) -> None:
    tc, _ = _make_client(tmp_path)
    r = tc.post(
        "/api/holds",
        params={"token": TOKEN},
        headers={**LOCALHOST_HEADERS, "content-type": "application/json"},
        json={"registry": "reg1", "reason": "SEC exam 2026", "duration_days": 90},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["hold_id"].startswith("hold-")
    assert data["registry"] == "reg1"
    assert data["reason"] == "SEC exam 2026"
    assert data["duration_days"] == 90
    # Verify file was written
    holds_file = tmp_path / ".novafabric" / "registries" / "reg1" / "holds.jsonl"
    assert holds_file.exists()
    record = json.loads(holds_file.read_text().strip())
    assert record["released_at"] is None


def test_create_and_list_hold(tmp_path: Path) -> None:
    tc, _ = _make_client(tmp_path)
    tc.post(
        "/api/holds",
        params={"token": TOKEN},
        headers={**LOCALHOST_HEADERS, "content-type": "application/json"},
        json={"registry": "reg2", "reason": "legal hold"},
    )
    r = tc.get("/api/holds", params={"token": TOKEN}, headers=LOCALHOST_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["total_active"] == 1
    reg = next(x for x in data["registries"] if x["name"] == "reg2")
    assert len(reg["holds"]) == 1
    assert reg["holds"][0]["reason"] == "legal hold"
    assert reg["holds"][0]["duration_days"] is None


def test_release_hold(tmp_path: Path) -> None:
    tc, _ = _make_client(tmp_path)
    create_r = tc.post(
        "/api/holds",
        params={"token": TOKEN},
        headers={**LOCALHOST_HEADERS, "content-type": "application/json"},
        json={"registry": "reg3", "reason": "test hold"},
    )
    hold_id = create_r.json()["hold_id"]

    release_r = tc.post(
        f"/api/holds/{hold_id}/release",
        params={"token": TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert release_r.status_code == 200
    assert release_r.json()["released"] is True
    assert release_r.json()["hold_id"] == hold_id

    # After release, list should show no active holds
    list_r = tc.get("/api/holds", params={"token": TOKEN}, headers=LOCALHOST_HEADERS)
    assert list_r.json()["total_active"] == 0


def test_release_nonexistent_hold(tmp_path: Path) -> None:
    tc, _ = _make_client(tmp_path)
    r = tc.post(
        "/api/holds/hold-nonexistent/release",
        params={"token": TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 404


def test_create_hold_path_traversal(tmp_path: Path) -> None:
    tc, _ = _make_client(tmp_path)
    r = tc.post(
        "/api/holds",
        params={"token": TOKEN},
        headers={**LOCALHOST_HEADERS, "content-type": "application/json"},
        json={"registry": "../etc/evil", "reason": "test"},
    )
    assert r.status_code == 400


def test_release_hold_path_traversal(tmp_path: Path) -> None:
    tc, _ = _make_client(tmp_path)
    r = tc.post(
        "/api/holds/../etc/release",
        params={"token": TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    # FastAPI path parameter will not match ../etc, so 404 or 400 both acceptable
    assert r.status_code in (400, 404, 422)


def test_holds_require_auth(tmp_path: Path) -> None:
    tc, _ = _make_client(tmp_path)
    assert tc.get("/api/holds", headers=LOCALHOST_HEADERS).status_code == 401
    assert tc.post(
        "/api/holds",
        headers={**LOCALHOST_HEADERS, "content-type": "application/json"},
        json={"registry": "r", "reason": "x"},
    ).status_code == 401
