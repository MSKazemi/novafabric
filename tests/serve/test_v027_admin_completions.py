"""Tests for v0.27.0 admin-completion endpoints.

Covers:
  POST /api/admin/roles              — assign-role (success + 422)
  DELETE /api/admin/roles/{s}/{r}    — revoke-role (success + 404)
  POST /api/admin/flush-jwks-cache   — flush JWKS cache (200)
  POST /api/db/upgrade               — db upgrade (200, head revision)
  POST /api/capsule-migrate          — capsule migrate (422 no source, 422 no output, 404 missing, success)
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
import yaml

fastapi_installed = pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-v027admin"
TOKEN_Q = f"token={VALID_TOKEN}"
H = {"host": "127.0.0.1:4321"}


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
# POST /api/admin/roles — assign-role
# ---------------------------------------------------------------------------


class TestAssignRoleEndpoint:
    def test_assign_role_success(self, client: TestClient) -> None:
        """Assign a valid role to a subject — should return 201 with ok=True."""
        r = client.post(
            f"/api/admin/roles?{TOKEN_Q}",
            headers=H,
            json={"subject": "user@example.com", "role": "reader"},
        )
        assert r.status_code == 201
        data = r.json()
        assert data["ok"] is True
        assert data["subject"] == "user@example.com"
        assert data["role"] == "reader"

    def test_assign_role_invalid_role_422(self, client: TestClient) -> None:
        """Assigning an unknown role should return 422."""
        r = client.post(
            f"/api/admin/roles?{TOKEN_Q}",
            headers=H,
            json={"subject": "user@example.com", "role": "superadmin"},
        )
        assert r.status_code == 422

    def test_assign_role_requires_auth(self, client: TestClient) -> None:
        """Missing token should return 401."""
        r = client.post(
            "/api/admin/roles",
            headers=H,
            json={"subject": "user@example.com", "role": "reader"},
        )
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# DELETE /api/admin/roles/{subject}/{role} — revoke-role
# ---------------------------------------------------------------------------


class TestRevokeRoleEndpoint:
    def test_revoke_role_success(self, client: TestClient) -> None:
        """Assign then revoke a role — revoke should return 200 with ok=True."""
        # First assign the role
        client.post(
            f"/api/admin/roles?{TOKEN_Q}",
            headers=H,
            json={"subject": "alice@example.com", "role": "writer"},
        )
        # Now revoke it
        r = client.delete(
            f"/api/admin/roles/alice%40example.com/writer?{TOKEN_Q}",
            headers=H,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["role"] == "writer"

    def test_revoke_role_not_found_404(self, client: TestClient) -> None:
        """Revoking a role that was never assigned should return 404."""
        r = client.delete(
            f"/api/admin/roles/nobody%40example.com/admin?{TOKEN_Q}",
            headers=H,
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/admin/flush-jwks-cache
# ---------------------------------------------------------------------------


class TestFlushJwksCacheEndpoint:
    def test_flush_jwks_cache_200(self, client: TestClient) -> None:
        """Flush JWKS cache should return 200 with ok=True."""
        r = client.post(f"/api/admin/flush-jwks-cache?{TOKEN_Q}", headers=H)
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert "note" in data

    def test_flush_jwks_cache_requires_auth(self, client: TestClient) -> None:
        """Missing token should return 401."""
        r = client.post("/api/admin/flush-jwks-cache", headers=H)
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/db/upgrade
# ---------------------------------------------------------------------------


class TestDbUpgradeEndpoint:
    def test_db_upgrade_head_200(self, client: TestClient) -> None:
        """DB upgrade with head revision should return 200 (ImportError path is ok)."""
        r = client.post(f"/api/db/upgrade?{TOKEN_Q}", headers=H, json={})
        assert r.status_code == 200
        data = r.json()
        assert "ok" in data
        assert "note" in data
        # revision defaults to 'head'
        assert data.get("revision") == "head"

    def test_db_upgrade_custom_revision(self, client: TestClient) -> None:
        """DB upgrade with a custom revision returns that revision in response."""
        r = client.post(
            f"/api/db/upgrade?{TOKEN_Q}",
            headers=H,
            json={"revision": "abc123"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data.get("revision") == "abc123"

    def test_db_upgrade_requires_auth(self, client: TestClient) -> None:
        """Missing token should return 401."""
        r = client.post("/api/db/upgrade", headers=H, json={})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/capsule-migrate
# ---------------------------------------------------------------------------


def _make_v01_capsule(path: Path) -> Path:
    """Create a minimal v0.1.x capsule directory."""
    path.mkdir(parents=True)
    manifest = {
        "schema_version": "0.1.0",
        "run_id": "01TESTMIGRATE",
        "status": "success",
        "model_call_count": 1,
        "tool_call_count": 0,
        "duration_ms": 1000,
        "capture_mode": "direct",
        "novafabric_version": "0.1.0",
    }
    (path / "capsule.yaml").write_text(yaml.dump(manifest))
    (path / "model-calls.jsonl").write_text("")
    (path / "tool-calls.jsonl").write_text("")
    return path


class TestCapsuleMigrateEndpoint:
    def test_migrate_missing_source_422(self, client: TestClient) -> None:
        """Missing source field should return 422."""
        r = client.post(
            f"/api/capsule-migrate?{TOKEN_Q}",
            headers=H,
            json={"output": "/tmp/out"},
        )
        assert r.status_code == 422

    def test_migrate_missing_output_422(self, client: TestClient) -> None:
        """Missing output field should return 422."""
        r = client.post(
            f"/api/capsule-migrate?{TOKEN_Q}",
            headers=H,
            json={"source": "/tmp/source"},
        )
        assert r.status_code == 422

    def test_migrate_nonexistent_source_404(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """Source path that does not exist should return 404."""
        r = client.post(
            f"/api/capsule-migrate?{TOKEN_Q}",
            headers=H,
            json={
                "source": str(tmp_path / "no-such-capsule"),
                "output": str(tmp_path / "out"),
            },
        )
        assert r.status_code == 404

    def test_migrate_success(self, client: TestClient, tmp_path: Path) -> None:
        """Valid v0.1.x capsule migrates successfully and returns ok=True."""
        source = _make_v01_capsule(tmp_path / "capsule-v01")
        output = tmp_path / "capsule-v100"

        r = client.post(
            f"/api/capsule-migrate?{TOKEN_Q}",
            headers=H,
            json={"source": str(source), "output": str(output)},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["note"] == "Migration complete"
        assert isinstance(data.get("files_migrated"), int)
        assert output.exists()

    def test_migrate_requires_auth(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """Missing token should return 401."""
        r = client.post(
            "/api/capsule-migrate",
            headers=H,
            json={"source": "/tmp/x", "output": "/tmp/y"},
        )
        assert r.status_code == 401
