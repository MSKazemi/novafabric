"""Tests for the /api/lineage/time-travel/{ref} endpoint (DD-6)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novafabric.serve.app import create_app

TOKEN = "testtoken"
LOCALHOST_HEADERS = {"host": "127.0.0.1:4321"}


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()
    db_path = tmp_path / "registry.db"
    app = create_app(token=TOKEN, capsule_dir=capsule_dir, db_path=db_path)
    return TestClient(app)


def test_time_travel_returns_200(client: TestClient) -> None:
    resp = client.get(
        "/api/lineage/time-travel/my-ref",
        params={"token": TOKEN, "at": "2026-01-01T00:00:00Z"},
        headers=LOCALHOST_HEADERS,
    )
    assert resp.status_code == 200


def test_time_travel_response_shape(client: TestClient) -> None:
    resp = client.get(
        "/api/lineage/time-travel/my-ref",
        params={"token": TOKEN, "at": "2026-01-01T00:00:00Z"},
        headers=LOCALHOST_HEADERS,
    )
    data = resp.json()
    assert "ref" in data
    assert "at" in data
    assert "depth" in data
    assert "supported" in data
    assert "ancestors" in data


def test_time_travel_ancestors_is_list(client: TestClient) -> None:
    resp = client.get(
        "/api/lineage/time-travel/my-ref",
        params={"token": TOKEN, "at": "2026-01-01T00:00:00Z"},
        headers=LOCALHOST_HEADERS,
    )
    data = resp.json()
    assert isinstance(data["ancestors"], list)


def test_time_travel_supported_is_bool(client: TestClient) -> None:
    resp = client.get(
        "/api/lineage/time-travel/my-ref",
        params={"token": TOKEN, "at": "2026-01-01T00:00:00Z"},
        headers=LOCALHOST_HEADERS,
    )
    data = resp.json()
    assert isinstance(data["supported"], bool)


def test_time_travel_ref_echoed(client: TestClient) -> None:
    resp = client.get(
        "/api/lineage/time-travel/some-asset@1.0",
        params={"token": TOKEN, "at": "2026-06-01T00:00:00Z"},
        headers=LOCALHOST_HEADERS,
    )
    data = resp.json()
    assert data["ref"] == "some-asset@1.0"


def test_time_travel_at_echoed(client: TestClient) -> None:
    at = "2026-03-15T12:00:00Z"
    resp = client.get(
        "/api/lineage/time-travel/my-ref",
        params={"token": TOKEN, "at": at},
        headers=LOCALHOST_HEADERS,
    )
    data = resp.json()
    assert data["at"] == at


def test_time_travel_depth_default(client: TestClient) -> None:
    resp = client.get(
        "/api/lineage/time-travel/my-ref",
        params={"token": TOKEN, "at": "2026-01-01T00:00:00Z"},
        headers=LOCALHOST_HEADERS,
    )
    data = resp.json()
    assert data["depth"] == 5


def test_time_travel_custom_depth(client: TestClient) -> None:
    resp = client.get(
        "/api/lineage/time-travel/my-ref",
        params={"token": TOKEN, "at": "2026-01-01T00:00:00Z", "depth": "10"},
        headers=LOCALHOST_HEADERS,
    )
    data = resp.json()
    assert data["depth"] == 10


def test_time_travel_requires_at_param(client: TestClient) -> None:
    resp = client.get(
        "/api/lineage/time-travel/my-ref",
        params={"token": TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert resp.status_code == 422


def test_time_travel_requires_token(client: TestClient) -> None:
    resp = client.get(
        "/api/lineage/time-travel/my-ref",
        params={"at": "2026-01-01T00:00:00Z"},
        headers=LOCALHOST_HEADERS,
    )
    assert resp.status_code == 401
