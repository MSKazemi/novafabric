"""Serve read endpoint for the dashboard admin console (ADR-0193 slice 2).

``GET /api/admin/api-keys`` returns a read-only, secret-free projection of the
API-key store for the Layer A/B dashboard. The dashboard never creates or
revokes keys from serve — those flows live behind the RBAC-gated
``/v0/api-keys`` server resource.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novafabric.serve.app import create_app
from novafabric.server.api_keys import create_key, parse_key, revoke_key

TOKEN = "testtoken"
H = {"host": "127.0.0.1:4321"}

_SHAPE_KEYS = {
    "key_id",
    "owner",
    "roles",
    "workspace",
    "created_at",
    "expires_at",
    "last_used_at",
    "revoked_at",
    "status",
}


@pytest.fixture(autouse=True)
def _tmp_audit_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "audit.jsonl"
    from novafabric.audit import _paths

    monkeypatch.setattr(_paths, "AUDIT_LOG_PATH", path)
    return path


def _client(tmp_path: Path, db: Path | None) -> TestClient:
    base = tmp_path / "capsules"
    base.mkdir(exist_ok=True)
    app = create_app(token=TOKEN, capsule_dir=base, db_path=db, static_dir=None)
    return TestClient(app)


def test_requires_token(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    create_key("alice@x", ["reader"], actor="seed", db_path=db)
    client = _client(tmp_path, db)
    r = client.get("/api/admin/api-keys", headers=H)
    assert r.status_code == 401


def test_shape_and_no_secrets(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    key, _ = create_key(
        "alice@x", ["reader", "writer"], actor="seed", db_path=db, workspace="ml"
    )
    _, secret = parse_key(key)  # type: ignore[misc]
    client = _client(tmp_path, db)

    r = client.get("/api/admin/api-keys", params={"token": TOKEN}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"keys", "total", "truncated"}
    assert body["total"] == 1
    assert body["truncated"] is False
    row = body["keys"][0]
    assert set(row) == _SHAPE_KEYS
    assert row["owner"] == "alice@x"
    assert row["roles"] == ["reader", "writer"]
    assert row["workspace"] == "ml"
    assert row["status"] == "active"
    # never secrets or hashes
    dumped = r.text
    assert secret not in dumped
    assert "secret_sha256" not in dumped
    assert hashlib.sha256(secret.encode()).hexdigest() not in dumped


def test_status_revoked(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    _, record = create_key("alice@x", ["reader"], actor="seed", db_path=db)
    revoke_key(record["key_id"], actor="seed", db_path=db)
    client = _client(tmp_path, db)
    r = client.get("/api/admin/api-keys", params={"token": TOKEN}, headers=H)
    rows = {x["key_id"]: x for x in r.json()["keys"]}
    assert rows[record["key_id"]]["status"] == "revoked"
    assert rows[record["key_id"]]["revoked_at"] is not None


def test_status_expired(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    _, record = create_key(
        "alice@x", ["reader"], actor="seed", db_path=db, expires_in_days=30
    )
    import sqlite3

    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE api_keys SET expires_at = ? WHERE key_id = ?",
        (past, record["key_id"]),
    )
    conn.commit()
    conn.close()
    client = _client(tmp_path, db)
    r = client.get("/api/admin/api-keys", params={"token": TOKEN}, headers=H)
    rows = {x["key_id"]: x for x in r.json()["keys"]}
    assert rows[record["key_id"]]["status"] == "expired"


def test_empty_no_db_returns_empty(tmp_path: Path) -> None:
    client = _client(tmp_path, tmp_path / "does-not-exist.db")
    r = client.get("/api/admin/api-keys", params={"token": TOKEN}, headers=H)
    assert r.status_code == 200
    assert r.json() == {"keys": [], "total": 0, "truncated": False}


def test_empty_none_db_returns_empty(tmp_path: Path) -> None:
    client = _client(tmp_path, None)
    r = client.get("/api/admin/api-keys", params={"token": TOKEN}, headers=H)
    assert r.status_code == 200
    assert r.json() == {"keys": [], "total": 0, "truncated": False}
