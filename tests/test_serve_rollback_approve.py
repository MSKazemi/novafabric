"""Tests for DD-3 (rollback endpoint) and DD-4 (approval endpoints)."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from novafabric.serve import audit
from novafabric.serve.app import create_app

VALID_TOKEN = "dd3-dd4-test-token-1234567890"
HEADERS = {"host": "127.0.0.1:4321"}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Path]]:
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()
    db = tmp_path / "registry.db"
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv(audit.AUDIT_ENV, str(audit_path))
    yield {"capsule_dir": capsule_dir, "db": db, "audit": audit_path, "tmp": tmp_path}


@pytest.fixture
def client(env: dict[str, Path]) -> Iterator[TestClient]:
    app = create_app(
        token=VALID_TOKEN,
        capsule_dir=env["capsule_dir"],
        db_path=env["db"],
        static_dir=None,
    )
    with TestClient(app) as c:
        yield c


def _register_asset(client: TestClient, name: str, version: str, asset_type: str = "model") -> str:
    """Register an asset and return its id."""
    spec_yaml = f"""\
novafabric_spec_version: "1"
asset_type: {asset_type}
name: {name}
version: {version}
status: development
description: test asset
spec:
  framework: pytorch
  artifact_path: /tmp/{name}
"""
    res = client.post(
        f"/api/assets?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"spec_yaml": spec_yaml, "confirmed": True},
    )
    assert res.status_code == 200, res.text
    return res.json()["asset"]["id"]


def _promote_asset(client: TestClient, asset_id: str, to_status: str) -> None:
    res = client.post(
        f"/api/assets/{asset_id}/promote?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"to_status": to_status, "confirmed": True, "force": True},
    )
    assert res.status_code == 200, res.text


# ============================================================
# DD-3: Rollback endpoint
# ============================================================


def test_rollback_requires_confirmation(client: TestClient) -> None:
    res = client.post(
        f"/api/assets/some-asset/rollback?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"reason": "test", "confirmed": False},
    )
    assert res.status_code == 400
    assert "confirmation required" in res.json()["detail"]


def test_rollback_unknown_asset_returns_409(client: TestClient) -> None:
    """An asset with no production version yields a 409 (RollbackError)."""
    res = client.post(
        f"/api/assets/nonexistent-asset/rollback?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"reason": "", "confirmed": True},
    )
    # No production version → RollbackError → 409
    assert res.status_code == 409


def test_rollback_requires_token(client: TestClient) -> None:
    res = client.post(
        "/api/assets/any-asset/rollback",
        headers=HEADERS,
        json={"confirmed": True},
    )
    assert res.status_code == 401


def test_rollback_success(client: TestClient, env: dict[str, Path]) -> None:
    """Set up production + archived versions directly in DB; rollback should restore archived."""
    from novafabric.registry.store import get_connection, init_schema
    from novafabric.spec.models import AssetStatus

    # Register two versions
    id_v1 = _register_asset(client, "rollback-model", "1.0.0")
    id_v2 = _register_asset(client, "rollback-model", "2.0.0")

    # Set v1 to archived (was previously in production), v2 to production
    conn = get_connection(env["db"])
    init_schema(conn)
    conn.execute(
        "UPDATE assets SET status = ? WHERE id = ?",
        (AssetStatus.archived.value, id_v1),
    )
    conn.execute(
        "UPDATE assets SET status = ?, promoted_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
        (AssetStatus.production.value, id_v2),
    )
    conn.commit()
    conn.close()

    # Rollback: v2 should be archived, v1 restored to production
    res = client.post(
        f"/api/assets/rollback-model/rollback?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"reason": "regression found", "confirmed": True},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    result = body["result"]
    assert result["archived_version"] == "2.0.0"
    assert result["restored_version"] == "1.0.0"
    assert "rollback_id" in result


def test_rollback_no_previous_version_returns_409(client: TestClient) -> None:
    """A single production version with no history yields 409."""
    id_v1 = _register_asset(client, "solo-model", "1.0.0")
    _promote_asset(client, id_v1, "staging")
    _promote_asset(client, id_v1, "production")

    res = client.post(
        f"/api/assets/solo-model/rollback?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"confirmed": True},
    )
    # Only one version exists; rollback should fail with 409
    assert res.status_code == 409


# ============================================================
# DD-4: Approval endpoints
# ============================================================


def test_get_approvals_requires_token(client: TestClient) -> None:
    res = client.get("/api/assets/some-uuid/approvals", headers=HEADERS)
    assert res.status_code == 401


def test_get_approvals_unknown_asset_returns_empty(client: TestClient) -> None:
    """Unknown asset UUID → empty approvals list (not 404)."""
    res = client.get(
        f"/api/assets/00000000-0000-0000-0000-000000000000/approvals?token={VALID_TOKEN}",
        headers=HEADERS,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["approvals"] == []
    assert body["supported"] is True


def test_get_approvals_empty_for_new_asset(client: TestClient) -> None:
    """A freshly registered asset has no approvals yet."""
    asset_id = _register_asset(client, "approval-model", "1.0.0")
    # Promote to pending_approval status (staging → pending_approval not directly available;
    # use staging as the closest available status for this test since the real
    # approve endpoint only accepts pending_approval)
    _promote_asset(client, asset_id, "staging")

    res = client.get(
        f"/api/assets/{asset_id}/approvals?token={VALID_TOKEN}",
        headers=HEADERS,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["asset_id"] == asset_id
    assert body["approvals"] == []
    assert body["required"] == 1
    assert body["supported"] is True


def test_approve_requires_confirmation(client: TestClient) -> None:
    res = client.post(
        f"/api/assets/some-uuid/approve?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"role": "reviewer", "confirmed": False},
    )
    assert res.status_code == 400
    assert "confirmation required" in res.json()["detail"]


def test_approve_requires_token(client: TestClient) -> None:
    res = client.post(
        "/api/assets/some-uuid/approve",
        headers=HEADERS,
        json={"role": "reviewer", "confirmed": True},
    )
    assert res.status_code == 401


def test_approve_unknown_asset_returns_404(client: TestClient) -> None:
    res = client.post(
        f"/api/assets/00000000-0000-0000-0000-000000000000/approve?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"role": "reviewer", "confirmed": True},
    )
    assert res.status_code == 404


def test_approve_wrong_status_returns_409(client: TestClient) -> None:
    """approve_asset() requires pending_approval status; staging returns 409."""
    asset_id = _register_asset(client, "staging-model", "1.0.0")
    _promote_asset(client, asset_id, "staging")

    res = client.post(
        f"/api/assets/{asset_id}/approve?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"role": "reviewer", "confirmed": True},
    )
    assert res.status_code == 409


def test_approve_success_and_list(client: TestClient, env: dict[str, Path]) -> None:
    """Full round-trip: register → promote to pending_approval → approve → list."""
    from novafabric.registry.store import get_connection, init_schema
    from novafabric.spec.models import AssetStatus

    asset_id = _register_asset(client, "pending-model", "1.0.0")

    # Manually set the asset to pending_approval status (the status needed for approve)
    conn = get_connection(env["db"])
    init_schema(conn)
    conn.execute(
        "UPDATE assets SET status = ? WHERE id = ?",
        (AssetStatus.pending_approval.value, asset_id),
    )
    conn.commit()
    conn.close()

    # Record an approval
    res = client.post(
        f"/api/assets/{asset_id}/approve?token={VALID_TOKEN}",
        headers=HEADERS,
        json={"role": "security", "actor": "alice", "note": "LGTM", "confirmed": True},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["role"] == "security"

    # List approvals — should show the one we just recorded
    res2 = client.get(
        f"/api/assets/{asset_id}/approvals?token={VALID_TOKEN}",
        headers=HEADERS,
    )
    assert res2.status_code == 200
    body2 = res2.json()
    assert len(body2["approvals"]) == 1
    ap = body2["approvals"][0]
    assert ap["actor"] == "alice"
    assert ap["note"] == "LGTM"
    assert "approved_at" in ap
