"""Tests for DD-7: /api/storage/stats and /api/storage/manifest-chain endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novafabric.serve.app import create_app

TOKEN = "testtoken"
AUTH = {"token": TOKEN}
LOCALHOST_HEADERS = {"host": "127.0.0.1:4321"}


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()
    db_path = tmp_path / "registry.db"
    app = create_app(token=TOKEN, capsule_dir=capsule_dir, db_path=db_path)
    return TestClient(app)


# ---------- /api/storage/stats ----------


def test_storage_stats_not_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Returns configured=false when neither NOVA_OBJECT_STORE_PATH nor NOVA_S3_BUCKET is set."""
    monkeypatch.delenv("NOVA_OBJECT_STORE_PATH", raising=False)
    monkeypatch.delenv("NOVA_S3_BUCKET", raising=False)
    resp = client.get("/api/storage/stats", params=AUTH, headers=LOCALHOST_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["configured"] is False
    assert "backend_type" in data


def test_storage_stats_requires_token(client: TestClient) -> None:
    resp = client.get("/api/storage/stats", headers=LOCALHOST_HEADERS)
    assert resp.status_code == 401


def test_storage_stats_configured_no_wal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When NOVA_OBJECT_STORE_PATH is set but no WAL dir exists, returns configured=true."""
    monkeypatch.setenv("NOVA_OBJECT_STORE_PATH", str(tmp_path / "store"))
    monkeypatch.delenv("NOVA_S3_BUCKET", raising=False)
    resp = client.get("/api/storage/stats", params=AUTH, headers=LOCALHOST_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["configured"] is True
    assert "backend_type" in data


def test_storage_stats_s3_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When NOVA_S3_BUCKET is set, returns configured=true."""
    monkeypatch.delenv("NOVA_OBJECT_STORE_PATH", raising=False)
    monkeypatch.setenv("NOVA_S3_BUCKET", "my-test-bucket")
    resp = client.get("/api/storage/stats", params=AUTH, headers=LOCALHOST_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["configured"] is True


def test_storage_stats_backend_type_env(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NOVA_OBJECT_STORE_BACKEND env var appears in the response."""
    monkeypatch.delenv("NOVA_OBJECT_STORE_PATH", raising=False)
    monkeypatch.delenv("NOVA_S3_BUCKET", raising=False)
    monkeypatch.setenv("NOVA_OBJECT_STORE_BACKEND", "s3")
    resp = client.get("/api/storage/stats", params=AUTH, headers=LOCALHOST_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["backend_type"] == "s3"


def test_storage_stats_default_backend_type(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default backend_type is local-wal."""
    monkeypatch.delenv("NOVA_OBJECT_STORE_PATH", raising=False)
    monkeypatch.delenv("NOVA_S3_BUCKET", raising=False)
    monkeypatch.delenv("NOVA_OBJECT_STORE_BACKEND", raising=False)
    resp = client.get("/api/storage/stats", params=AUTH, headers=LOCALHOST_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["backend_type"] == "local-wal"


# ---------- /api/storage/manifest-chain ----------


def test_manifest_chain_not_configured(
    client: TestClient,
) -> None:
    """Returns entries list (may be empty) when WAL dir doesn't exist."""
    resp = client.get("/api/storage/manifest-chain", params=AUTH, headers=LOCALHOST_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert "entries" in data
    assert isinstance(data["entries"], list)


def test_manifest_chain_requires_token(client: TestClient) -> None:
    resp = client.get("/api/storage/manifest-chain", headers=LOCALHOST_HEADERS)
    assert resp.status_code == 401


def test_manifest_chain_empty_entries(
    client: TestClient,
) -> None:
    """When no manifest files are present, returns an empty entries list."""
    resp = client.get(
        "/api/storage/manifest-chain",
        params={**AUTH, "limit": "5"},
        headers=LOCALHOST_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["entries"], list)


def test_manifest_chain_limit_param_valid(client: TestClient) -> None:
    """limit=1 is accepted."""
    resp = client.get(
        "/api/storage/manifest-chain",
        params={**AUTH, "limit": "1"},
        headers=LOCALHOST_HEADERS,
    )
    assert resp.status_code == 200


def test_manifest_chain_limit_too_large(client: TestClient) -> None:
    """limit>100 is rejected."""
    resp = client.get(
        "/api/storage/manifest-chain",
        params={**AUTH, "limit": "101"},
        headers=LOCALHOST_HEADERS,
    )
    assert resp.status_code == 422


def test_manifest_chain_limit_too_small(client: TestClient) -> None:
    """limit=0 is rejected."""
    resp = client.get(
        "/api/storage/manifest-chain",
        params={**AUTH, "limit": "0"},
        headers=LOCALHOST_HEADERS,
    )
    assert resp.status_code == 422
