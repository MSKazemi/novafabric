"""Tests for /api/admin/roles in the experimental serve app (DA-1, ADR-0060)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

LOCALHOST_HEADERS = {"host": "127.0.0.1:4321"}
TOKEN = "test-token-roles-da1"


def _make_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Create a TestClient with HOME, registry DB, and audit log redirected to tmp_path."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # type: ignore[attr-defined]
    monkeypatch.delenv("NOVA_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("NOVA_OIDC_CLIENT_ID", raising=False)

    audit_file = tmp_path / "audit.jsonl"
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(audit_file))

    db = tmp_path / "registry.db"
    from novafabric.registry.store import get_connection, init_schema

    conn = get_connection(db)
    init_schema(conn)
    conn.close()

    app = create_app(
        token=TOKEN,
        capsule_dir=tmp_path / "runs",
        db_path=db,
    )
    return TestClient(app)


def _read_audit(tmp_path: Path) -> list[dict]:
    audit_file = tmp_path / "audit.jsonl"
    if not audit_file.exists():
        return []
    return [json.loads(line) for line in audit_file.read_text().splitlines() if line]


# ---------- GET /api/admin/roles ----------


def test_list_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tc = _make_client(tmp_path, monkeypatch)
    r = tc.get(
        "/api/admin/roles",
        params={"token": TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["roles"] == []
    assert data["server_mode"] is False
    assert "Role management requires server mode" in data["message"]


def test_list_requires_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tc = _make_client(tmp_path, monkeypatch)
    r = tc.get("/api/admin/roles", headers=LOCALHOST_HEADERS)
    assert r.status_code == 401


def test_list_includes_effective_now_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tc = _make_client(tmp_path, monkeypatch)
    tc.post(
        "/api/admin/roles",
        params={"token": TOKEN},
        json={"subject": "alice@example.com", "role": "writer"},
        headers=LOCALHOST_HEADERS,
    )
    r = tc.get(
        "/api/admin/roles",
        params={"token": TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 200
    roles = r.json()["roles"]
    assert len(roles) == 1
    # No OIDC configured → local mode → effective_now is True
    assert roles[0]["effective_now"] is True


def test_list_effective_now_false_when_oidc_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tc = _make_client(tmp_path, monkeypatch)
    tc.post(
        "/api/admin/roles",
        params={"token": TOKEN},
        json={"subject": "alice@example.com", "role": "writer"},
        headers=LOCALHOST_HEADERS,
    )
    monkeypatch.setenv("NOVA_OIDC_ISSUER", "https://issuer.example.com")

    r = tc.get(
        "/api/admin/roles",
        params={"token": TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["server_mode"] is True
    assert data["roles"][0]["effective_now"] is False


# ---------- POST /api/admin/roles ----------


def test_assign_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tc = _make_client(tmp_path, monkeypatch)
    r = tc.post(
        "/api/admin/roles",
        params={"token": TOKEN},
        json={"subject": "alice@example.com", "role": "writer"},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["ok"] is True
    assert body["subject"] == "alice@example.com"
    assert body["role"] == "writer"
    assert body["assigned_by"].startswith("local:")


def test_assign_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tc = _make_client(tmp_path, monkeypatch)
    payload = {"subject": "alice@example.com", "role": "writer"}
    r1 = tc.post(
        "/api/admin/roles",
        params={"token": TOKEN},
        json=payload,
        headers=LOCALHOST_HEADERS,
    )
    r2 = tc.post(
        "/api/admin/roles",
        params={"token": TOKEN},
        json=payload,
        headers=LOCALHOST_HEADERS,
    )
    assert r1.status_code == 201
    assert r2.status_code == 201

    r_list = tc.get(
        "/api/admin/roles",
        params={"token": TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    roles = r_list.json()["roles"]
    matching = [
        x for x in roles
        if x["subject"] == "alice@example.com" and x["role"] == "writer"
    ]
    assert len(matching) == 1


def test_assign_requires_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tc = _make_client(tmp_path, monkeypatch)
    r = tc.post(
        "/api/admin/roles",
        json={"subject": "alice@example.com", "role": "writer"},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 401


def test_assign_invalid_role_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tc = _make_client(tmp_path, monkeypatch)
    r = tc.post(
        "/api/admin/roles",
        params={"token": TOKEN},
        json={"subject": "alice@example.com", "role": "superduper"},
        headers=LOCALHOST_HEADERS,
    )
    # Pydantic validates the enum and rejects unknown roles with 422
    assert r.status_code == 422


def test_assign_writes_audit_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tc = _make_client(tmp_path, monkeypatch)
    tc.post(
        "/api/admin/roles",
        params={"token": TOKEN},
        json={"subject": "alice@example.com", "role": "writer"},
        headers=LOCALHOST_HEADERS,
    )
    entries = _read_audit(tmp_path)
    matching = [
        e for e in entries
        if e["action"] == "assign_role"
        and e["args"]["subject"] == "alice@example.com"
    ]
    assert len(matching) == 1
    assert (
        matching[0]["cli_equivalent"]
        == "nova server assign-role alice@example.com writer"
    )
    assert matching[0]["result"] == "ok"
    assert matching[0]["actor_token_fp"] == TOKEN[:8]


# ---------- DELETE /api/admin/roles/{subject}/{role} ----------


def test_revoke_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tc = _make_client(tmp_path, monkeypatch)
    tc.post(
        "/api/admin/roles",
        params={"token": TOKEN},
        json={"subject": "alice@example.com", "role": "writer"},
        headers=LOCALHOST_HEADERS,
    )
    # Need a second admin so we are not blocked by the lockout invariant; we are
    # revoking writer here so the lockout guard does not apply, but include it
    # anyway for realism.
    tc.post(
        "/api/admin/roles",
        params={"token": TOKEN},
        json={"subject": "admin@example.com", "role": "admin"},
        headers=LOCALHOST_HEADERS,
    )
    r = tc.delete(
        "/api/admin/roles/alice@example.com/writer",
        params={"token": TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["subject"] == "alice@example.com"
    assert body["role"] == "writer"


def test_revoke_missing_returns_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tc = _make_client(tmp_path, monkeypatch)
    r = tc.delete(
        "/api/admin/roles/ghost@example.com/reader",
        params={"token": TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 404


def test_revoke_requires_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tc = _make_client(tmp_path, monkeypatch)
    r = tc.delete(
        "/api/admin/roles/alice@example.com/writer",
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 401


def test_revoke_last_admin_blocked_returns_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tc = _make_client(tmp_path, monkeypatch)
    # Only one admin assignment exists; no OIDC configured.
    tc.post(
        "/api/admin/roles",
        params={"token": TOKEN},
        json={"subject": "solo@example.com", "role": "admin"},
        headers=LOCALHOST_HEADERS,
    )
    r = tc.delete(
        "/api/admin/roles/solo@example.com/admin",
        params={"token": TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    assert r.status_code == 409
    assert "last admin" in r.json()["detail"].lower()


def test_revoke_writes_audit_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tc = _make_client(tmp_path, monkeypatch)
    tc.post(
        "/api/admin/roles",
        params={"token": TOKEN},
        json={"subject": "alice@example.com", "role": "writer"},
        headers=LOCALHOST_HEADERS,
    )
    tc.delete(
        "/api/admin/roles/alice@example.com/writer",
        params={"token": TOKEN},
        headers=LOCALHOST_HEADERS,
    )
    entries = _read_audit(tmp_path)
    matching = [
        e for e in entries
        if e["action"] == "revoke_role"
        and e["args"]["subject"] == "alice@example.com"
    ]
    assert len(matching) == 1
    assert (
        matching[0]["cli_equivalent"]
        == "nova server revoke-role alice@example.com writer"
    )
    assert matching[0]["result"] == "ok"
