"""Tests for the admin endpoints in the experimental serve app (DD-8)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novafabric.serve.app import create_app

LOCALHOST_HEADERS = {"host": "127.0.0.1:4321"}
TOKEN = "test-token-admin-dd8"


def _make_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Create a TestClient with tokens.jsonl redirected to tmp_path."""
    nova_dir = tmp_path / ".novafabric"
    nova_dir.mkdir(parents=True, exist_ok=True)

    # Redirect Path.home() so tokens.jsonl is written into tmp_path
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # type: ignore[attr-defined]

    app = create_app(
        token=TOKEN,
        capsule_dir=tmp_path / "runs",
        db_path=None,
    )
    return TestClient(app)


# ---------- GET /api/admin/tokens ----------


def test_list_tokens_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tc = _make_client(tmp_path, monkeypatch)
    r = tc.get("/api/admin/tokens", params={"token": TOKEN}, headers=LOCALHOST_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["tokens"] == []
    assert data["session_token_fingerprint"] == TOKEN[:8]


def test_list_tokens_requires_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tc = _make_client(tmp_path, monkeypatch)
    r = tc.get("/api/admin/tokens", headers=LOCALHOST_HEADERS)
    assert r.status_code == 401


# ---------- POST /api/admin/tokens ----------


def test_issue_token_confirmed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tc = _make_client(tmp_path, monkeypatch)
    r = tc.post(
        "/api/admin/tokens",
        params={"token": TOKEN},
        headers={**LOCALHOST_HEADERS, "content-type": "application/json"},
        json={"label": "test-label", "confirmed": True},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert len(data["token"]) > 20
    assert len(data["fingerprint"]) == 16
    assert data["label"] == "test-label"
    assert "Save this token" in data["warning"]

    # Verify the token was persisted
    tokens_file = tmp_path / ".novafabric" / "tokens.jsonl"
    assert tokens_file.exists()
    record = json.loads(tokens_file.read_text().strip().splitlines()[0])
    assert record["label"] == "test-label"
    assert record["fingerprint"] == data["fingerprint"]
    assert record["token"] == data["token"]
    assert record["revoked"] is False


def test_issue_token_without_confirmation_returns_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tc = _make_client(tmp_path, monkeypatch)
    r = tc.post(
        "/api/admin/tokens",
        params={"token": TOKEN},
        headers={**LOCALHOST_HEADERS, "content-type": "application/json"},
        json={"label": "should-fail", "confirmed": False},
    )
    assert r.status_code == 400
    assert "confirmation required" in r.json()["detail"]


def test_issue_token_default_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tc = _make_client(tmp_path, monkeypatch)
    r = tc.post(
        "/api/admin/tokens",
        params={"token": TOKEN},
        headers={**LOCALHOST_HEADERS, "content-type": "application/json"},
        json={"confirmed": True},
    )
    assert r.status_code == 200
    assert r.json()["label"] == "dashboard-issued"


# ---------- DELETE /api/admin/tokens/{fingerprint} ----------


def test_revoke_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tc = _make_client(tmp_path, monkeypatch)

    # First issue a token
    issue_r = tc.post(
        "/api/admin/tokens",
        params={"token": TOKEN},
        headers={**LOCALHOST_HEADERS, "content-type": "application/json"},
        json={"label": "to-revoke", "confirmed": True},
    )
    assert issue_r.status_code == 200
    fp = issue_r.json()["fingerprint"]

    # Revoke it
    revoke_r = tc.delete(
        f"/api/admin/tokens/{fp}",
        params={"token": TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert revoke_r.status_code == 200
    data = revoke_r.json()
    assert data["ok"] is True
    assert data["fingerprint"] == fp
    assert data["revoked"] is True

    # Verify file updated
    tokens_file = tmp_path / ".novafabric" / "tokens.jsonl"
    record = json.loads(tokens_file.read_text().strip().splitlines()[0])
    assert record["revoked"] is True


def test_revoke_nonexistent_token_returns_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tc = _make_client(tmp_path, monkeypatch)
    # Create the file first so the 404 is from "not found in file", not "file missing"
    tokens_file = tmp_path / ".novafabric" / "tokens.jsonl"
    tokens_file.parent.mkdir(parents=True, exist_ok=True)
    tokens_file.write_text(
        json.dumps({"label": "x", "fingerprint": "aaaa", "token": "t", "created_at": "", "revoked": False}) + "\n"
    )
    r = tc.delete(
        "/api/admin/tokens/does-not-exist",
        params={"token": TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 404


def test_revoke_token_requires_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tc = _make_client(tmp_path, monkeypatch)
    r = tc.delete("/api/admin/tokens/somefp", headers=LOCALHOST_HEADERS)
    assert r.status_code == 401


def test_list_shows_revoked_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tc = _make_client(tmp_path, monkeypatch)

    # Issue a token
    issue_r = tc.post(
        "/api/admin/tokens",
        params={"token": TOKEN},
        headers={**LOCALHOST_HEADERS, "content-type": "application/json"},
        json={"label": "revoke-and-list", "confirmed": True},
    )
    fp = issue_r.json()["fingerprint"]

    # Revoke it
    tc.delete(f"/api/admin/tokens/{fp}", params={"token": TOKEN}, headers=LOCALHOST_HEADERS)

    # List should show it as revoked, not omit it
    list_r = tc.get("/api/admin/tokens", params={"token": TOKEN}, headers=LOCALHOST_HEADERS)
    tokens = list_r.json()["tokens"]
    assert len(tokens) == 1
    assert tokens[0]["revoked"] is True
    assert tokens[0]["fingerprint"] == fp


# ---------- GET /api/admin/roles ----------


def test_list_roles_local_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Ensure OIDC env vars are not set
    monkeypatch.delenv("NOVA_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("NOVA_OIDC_CLIENT_ID", raising=False)

    tc = _make_client(tmp_path, monkeypatch)
    r = tc.get("/api/admin/roles", params={"token": TOKEN}, headers=LOCALHOST_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["server_mode"] is False
    assert data["roles"] == []
    assert "OIDC" in data["message"]


def test_list_roles_server_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_OIDC_ISSUER", "https://example.com/oidc")

    tc = _make_client(tmp_path, monkeypatch)
    r = tc.get("/api/admin/roles", params={"token": TOKEN}, headers=LOCALHOST_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["server_mode"] is True
    assert data["roles"] == []
    assert data["message"] == ""


def test_list_roles_requires_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tc = _make_client(tmp_path, monkeypatch)
    r = tc.get("/api/admin/roles", headers=LOCALHOST_HEADERS)
    assert r.status_code == 401
