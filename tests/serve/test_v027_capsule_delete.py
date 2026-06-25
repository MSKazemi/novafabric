"""Tests for v0.27.0 capsule delete endpoint.

Covers:
  DELETE /api/runs/{run_id} — delete a capsule directory subject to legal holds
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest
import yaml

fastapi_installed = pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-v027del"
TOKEN_Q = f"token={VALID_TOKEN}"
H = {"host": "127.0.0.1:4321"}


def _make_capsule(capsule_dir: Path, run_id: str) -> Path:
    """Create a minimal valid capsule directory."""
    cap = capsule_dir / run_id
    cap.mkdir(parents=True)
    manifest = {
        "schema_version": "1.0.0",
        "run_id": run_id,
        "status": "success",
        "model_call_count": 1,
        "tool_call_count": 0,
        "duration_ms": 1000,
        "capture_mode": "direct",
        "novafabric_version": "0.27.0",
    }
    (cap / "capsule.yaml").write_text(yaml.dump(manifest))
    (cap / "model-calls.jsonl").write_text("")
    (cap / "tool-calls.jsonl").write_text("")
    return cap


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    base = tmp_path / "runs"
    base.mkdir()
    return base


@pytest.fixture
def client(capsule_dir: Path, tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(
        capsule_dir=capsule_dir,
        token=VALID_TOKEN,
        db_path=tmp_path / "registry.db",
        static_dir=None,
    )
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# DELETE /api/runs/{run_id}
# ---------------------------------------------------------------------------


def test_delete_nonexistent_capsule_returns_404(client: TestClient) -> None:
    """Deleting a run_id that does not exist returns 404."""
    r = client.delete("/api/runs/no-such-run", params={"token": VALID_TOKEN}, headers=H)
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


def test_delete_invalid_run_id_with_dotdot_returns_400(client: TestClient) -> None:
    """run_id containing '..' is rejected with 400 (path traversal guard)."""
    r = client.delete("/api/runs/..%2Fetc%2Fpasswd", params={"token": VALID_TOKEN}, headers=H)
    assert r.status_code in (400, 404)  # 404 = path guard encoded, 400 = decoded guard


def test_delete_invalid_run_id_dotdot_decoded(client: TestClient) -> None:
    """run_id containing '..' as separate path segment returns 400."""
    r = client.delete("/api/runs/foo..bar", params={"token": VALID_TOKEN}, headers=H)
    # "foo..bar" contains ".." — blocked
    assert r.status_code == 400
    assert "invalid" in r.json()["detail"].lower()


def test_delete_blocked_by_active_legal_hold(
    capsule_dir: Path, tmp_path: Path
) -> None:
    """DELETE returns 409 when an active legal hold exists."""
    run_id = "run-hold-blocked"
    _make_capsule(capsule_dir, run_id)

    # Create an active hold in the registries directory
    reg_dir = capsule_dir.parent / "registries" / "default"
    reg_dir.mkdir(parents=True)
    hold_record = {
        "hold_id": "hold-abc123",
        "registry": "default",
        "reason": "test litigation hold",
        "duration_days": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "released_at": None,
    }
    (reg_dir / "holds.jsonl").write_text(json.dumps(hold_record) + "\n")

    app = create_app(
        capsule_dir=capsule_dir,
        token=VALID_TOKEN,
        db_path=tmp_path / "registry.db",
        static_dir=None,
    )
    with TestClient(app) as tc:
        r = tc.delete(f"/api/runs/{run_id}", params={"token": VALID_TOKEN}, headers=H)

    assert r.status_code == 409
    body = r.json()["detail"]
    assert "hold" in body.lower()
    assert "hold-abc123" in body
    # Capsule directory must still exist — deletion was blocked
    assert (capsule_dir / run_id).is_dir()


def test_delete_released_hold_allows_deletion(
    capsule_dir: Path, tmp_path: Path
) -> None:
    """DELETE succeeds when the only hold is released (released_at is set)."""
    run_id = "run-released-hold"
    _make_capsule(capsule_dir, run_id)

    reg_dir = capsule_dir.parent / "registries" / "default"
    reg_dir.mkdir(parents=True)
    hold_record = {
        "hold_id": "hold-released",
        "registry": "default",
        "reason": "old hold",
        "duration_days": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "released_at": "2026-02-01T00:00:00+00:00",
    }
    (reg_dir / "holds.jsonl").write_text(json.dumps(hold_record) + "\n")

    app = create_app(
        capsule_dir=capsule_dir,
        token=VALID_TOKEN,
        db_path=tmp_path / "registry.db",
        static_dir=None,
    )
    with TestClient(app) as tc:
        r = tc.delete(f"/api/runs/{run_id}", params={"token": VALID_TOKEN}, headers=H)

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["run_id"] == run_id
    assert not (capsule_dir / run_id).exists()


def test_delete_success_removes_directory(client: TestClient, capsule_dir: Path) -> None:
    """Successful DELETE removes the capsule directory and returns 200."""
    run_id = "run-to-delete"
    _make_capsule(capsule_dir, run_id)
    assert (capsule_dir / run_id).is_dir()

    r = client.delete(f"/api/runs/{run_id}", params={"token": VALID_TOKEN}, headers=H)

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["run_id"] == run_id
    assert "deleted" in body["note"].lower()
    # Directory must be gone
    assert not (capsule_dir / run_id).exists()


def test_delete_requires_auth(client: TestClient, capsule_dir: Path) -> None:
    """DELETE without a valid token returns 401."""
    run_id = "run-auth-guard"
    _make_capsule(capsule_dir, run_id)

    r = client.delete(f"/api/runs/{run_id}", params={"token": "wrong-token"}, headers=H)
    assert r.status_code == 401
    # Capsule should still exist
    assert (capsule_dir / run_id).is_dir()


def test_delete_force_flag_accepted(client: TestClient, capsule_dir: Path) -> None:
    """DELETE with force=true is accepted when no holds exist."""
    run_id = "run-force-delete"
    _make_capsule(capsule_dir, run_id)

    r = client.delete(
        f"/api/runs/{run_id}",
        params={"token": VALID_TOKEN, "force": "true"},
        headers=H,
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert not (capsule_dir / run_id).exists()


def test_delete_force_still_blocked_by_active_hold(
    capsule_dir: Path, tmp_path: Path
) -> None:
    """force=true does NOT bypass an active legal hold."""
    run_id = "run-force-hold-blocked"
    _make_capsule(capsule_dir, run_id)

    reg_dir = capsule_dir.parent / "registries" / "reg-alpha"
    reg_dir.mkdir(parents=True)
    hold_record = {
        "hold_id": "hold-force-block",
        "registry": "reg-alpha",
        "reason": "e-discovery",
        "duration_days": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "released_at": None,
    }
    (reg_dir / "holds.jsonl").write_text(json.dumps(hold_record) + "\n")

    app = create_app(
        capsule_dir=capsule_dir,
        token=VALID_TOKEN,
        db_path=tmp_path / "registry.db",
        static_dir=None,
    )
    with TestClient(app) as tc:
        r = tc.delete(
            f"/api/runs/{run_id}",
            params={"token": VALID_TOKEN, "force": "true"},
            headers=H,
        )

    assert r.status_code == 409
    assert (capsule_dir / run_id).is_dir()
