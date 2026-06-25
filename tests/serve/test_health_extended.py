"""Tests for the extended /api/health endpoint (DD-5)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novafabric.serve.app import create_app

LOCALHOST_HEADERS = {"host": "127.0.0.1:4321"}


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()
    db_path = tmp_path / "registry.db"
    app = create_app(token="testtoken", capsule_dir=capsule_dir, db_path=db_path)
    return TestClient(app)


def test_health_returns_ok(client: TestClient) -> None:
    resp = client.get("/api/health", headers=LOCALHOST_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True


def test_health_contains_new_fields(client: TestClient) -> None:
    resp = client.get("/api/health", headers=LOCALHOST_HEADERS)
    data = resp.json()
    for field in ("backend_type", "db_path", "keystore_ok", "opa_available", "novaseal_configured", "schema_version"):
        assert field in data, f"missing field: {field}"


def test_health_backend_type_is_string(client: TestClient) -> None:
    resp = client.get("/api/health", headers=LOCALHOST_HEADERS)
    data = resp.json()
    assert isinstance(data["backend_type"], str)
    assert data["backend_type"] in ("sqlite", "postgres")


def test_health_opa_available_is_bool(client: TestClient) -> None:
    resp = client.get("/api/health", headers=LOCALHOST_HEADERS)
    data = resp.json()
    assert isinstance(data["opa_available"], bool)


def test_health_keystore_ok_is_bool(client: TestClient) -> None:
    resp = client.get("/api/health", headers=LOCALHOST_HEADERS)
    data = resp.json()
    assert isinstance(data["keystore_ok"], bool)


def test_health_novaseal_configured_is_bool(client: TestClient) -> None:
    resp = client.get("/api/health", headers=LOCALHOST_HEADERS)
    data = resp.json()
    assert isinstance(data["novaseal_configured"], bool)


def test_health_schema_version(client: TestClient) -> None:
    resp = client.get("/api/health", headers=LOCALHOST_HEADERS)
    data = resp.json()
    assert data["schema_version"] == "0.1.0"


def test_health_db_path_is_string(client: TestClient) -> None:
    resp = client.get("/api/health", headers=LOCALHOST_HEADERS)
    data = resp.json()
    assert isinstance(data["db_path"], str)
    assert len(data["db_path"]) > 0


def test_health_unauthenticated(client: TestClient) -> None:
    """Health must not require a token."""
    resp = client.get("/api/health", headers=LOCALHOST_HEADERS)
    assert resp.status_code == 200


def test_health_sqlite_backend_by_default(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without NOVAFABRIC_DB_URL set, backend_type must be 'sqlite'."""
    monkeypatch.delenv("NOVAFABRIC_DB_URL", raising=False)
    resp = client.get("/api/health", headers=LOCALHOST_HEADERS)
    data = resp.json()
    assert data["backend_type"] == "sqlite"


def test_health_postgres_backend_when_env_set(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """With NOVAFABRIC_DB_URL set, backend_type must be 'postgres'."""
    monkeypatch.setenv("NOVAFABRIC_DB_URL", "postgresql://localhost/test")
    resp = client.get("/api/health", headers=LOCALHOST_HEADERS)
    data = resp.json()
    assert data["backend_type"] == "postgres"
