"""End-to-end auth integration tests (Track S-5).

Tests the full auth flow against the live server using:
1. No-auth mode (``auth_disabled``) — confirms all endpoints are accessible
   without any token.
2. Offline token round-trip — issue → verify at the token layer, then confirm
   that the no-auth server accepts anonymous admin access on every major route.

No real OIDC provider is required.  The offline-token subsystem is validated at
the token layer; OIDC is validated in unit tests (test_server_auth.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import ServerConfig  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def no_auth_client(tmp_path: Path) -> TestClient:
    """TestClient backed by a fresh SQLite DB with auth disabled (local mode)."""
    # ADR-0184: anonymous admin now requires the explicit insecure opt-out.
    cfg = ServerConfig(db_path=str(tmp_path / "auth-integ.db"), insecure_no_auth=True)
    app = create_app(cfg)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def offline_key(tmp_path: Path) -> Path:
    """Generate an ed25519 keypair and return the private key path."""
    from novafabric.server.offline_tokens import generate_keypair

    priv = tmp_path / "keys" / "offline.pem"
    generate_keypair(priv)
    return priv


# ---------------------------------------------------------------------------
# No-auth (auth_disabled) mode — all endpoints reachable without a token
# ---------------------------------------------------------------------------


class TestNoAuthMode:
    """Confirm the server works in local no-auth mode (no Bearer token needed)."""

    def test_health_accessible(self, no_auth_client: TestClient) -> None:
        resp = no_auth_client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_list_assets_accessible(self, no_auth_client: TestClient) -> None:
        resp = no_auth_client.get("/v0/assets")
        assert resp.status_code == 200

    def test_list_capsules_accessible(self, no_auth_client: TestClient) -> None:
        resp = no_auth_client.get("/v0/capsules")
        assert resp.status_code == 200

    def test_list_lineage_nodes_accessible(self, no_auth_client: TestClient) -> None:
        resp = no_auth_client.get("/v0/lineage/nodes")
        assert resp.status_code == 200

    def test_list_replays_not_found_accessible(self, no_auth_client: TestClient) -> None:
        """GET /v0/replays/{id} with a nonexistent id returns 404, not 401."""
        resp = no_auth_client.get("/v0/replays/no-such-replay")
        # 404 = auth passed; 401 = would indicate a broken auth bypass
        assert resp.status_code in (200, 404)

    def test_create_asset_accessible(self, no_auth_client: TestClient) -> None:
        spec_yaml = """\
novafabric_spec_version: "0.1"
asset_type: model
name: auth-integ-model
version: "1.0.0"
status: development
spec:
  framework: pytorch
  artifact_path: /tmp/auth-model.pt
"""
        resp = no_auth_client.post("/v0/assets", json={"spec_yaml": spec_yaml})
        assert resp.status_code == 201

    def test_flush_jwks_accessible_no_auth(self, no_auth_client: TestClient) -> None:
        """Admin-only endpoint must be accessible in no-auth local mode."""
        resp = no_auth_client.post("/v0/admin/flush-jwks")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Offline token subsystem — issue → verify → revoke round-trip
# ---------------------------------------------------------------------------


class TestOfflineTokenRoundTrip:
    """Validate the offline token lifecycle independently of the HTTP server.

    The offline token subsystem is self-contained; this confirms it works
    end-to-end with a real filesystem keypair and SQLite audit table.
    """

    def test_issue_verify_cycle(self, offline_key: Path, tmp_path: Path) -> None:
        from novafabric.server.offline_tokens import issue_token, verify_offline_token

        db = tmp_path / "tokens.db"
        token = issue_token(
            subject="integ-user@example.com",
            roles=["reader", "writer"],
            expires_in_days=1,
            key_path=offline_key,
            db_path=db,
        )

        ctx = verify_offline_token(token, offline_key, db_path=db)
        assert ctx.subject == "integ-user@example.com"
        assert "reader" in ctx.roles
        assert "writer" in ctx.roles

    def test_revoked_token_raises(self, offline_key: Path, tmp_path: Path) -> None:
        import jwt

        from novafabric.server.offline_tokens import (
            issue_token,
            revoke_token,
            verify_offline_token,
        )

        db = tmp_path / "tokens.db"
        token = issue_token(
            subject="revoke-me@example.com",
            roles=["reader"],
            expires_in_days=1,
            key_path=offline_key,
            db_path=db,
        )

        # Extract the JTI (token ID) without signature verification
        claims = jwt.decode(token, options={"verify_signature": False})
        jti = claims["jti"]

        # Revoke the token
        revoke_token(jti, offline_key, db_path=db)

        # Subsequent verification must raise PermissionError
        with pytest.raises(PermissionError, match="revoked"):
            verify_offline_token(token, offline_key, db_path=db)

    def test_multiple_tokens_independent(self, offline_key: Path, tmp_path: Path) -> None:
        from novafabric.server.offline_tokens import issue_token, verify_offline_token

        db = tmp_path / "tokens.db"
        token_a = issue_token(
            "a@example.com", ["reader"], 1, offline_key, db_path=db
        )
        token_b = issue_token(
            "b@example.com", ["admin"], 7, offline_key, db_path=db
        )

        ctx_a = verify_offline_token(token_a, offline_key, db_path=db)
        ctx_b = verify_offline_token(token_b, offline_key, db_path=db)

        assert ctx_a.subject == "a@example.com"
        assert ctx_b.subject == "b@example.com"
        assert "admin" not in ctx_a.roles
        assert "admin" in ctx_b.roles

    def test_token_with_all_roles(self, offline_key: Path, tmp_path: Path) -> None:
        from novafabric.server.offline_tokens import issue_token, verify_offline_token

        db = tmp_path / "tokens.db"
        token = issue_token(
            "super@example.com",
            ["reader", "writer", "admin", "auditor"],
            1,
            offline_key,
            db_path=db,
        )

        ctx = verify_offline_token(token, offline_key, db_path=db)
        for role in ("reader", "writer", "admin", "auditor"):
            assert ctx.has_role(role), f"Expected role {role!r} in {ctx.roles}"


# ---------------------------------------------------------------------------
# Auth disabled → no 401 on any resource endpoint
# ---------------------------------------------------------------------------


class TestNoTokenRequired:
    """Exhaustively verify that no resource endpoint returns 401 in no-auth mode."""

    _READONLY_ENDPOINTS: list[str] = [
        "/health",
        "/v0/assets",
        "/v0/capsules",
        "/v0/lineage/nodes",
        "/v0/lineage/blast-radius?ref=test",
        "/v0/lineage/provenance?ref=test",
        "/v0/lineage/replay-chain?run_id=test",
        # Replays has no list endpoint; GET by id returns 404 for unknown ids
        "/v0/replays/no-such-replay",
    ]

    @pytest.mark.parametrize("endpoint", _READONLY_ENDPOINTS)
    def test_no_401_on_get(
        self, no_auth_client: TestClient, endpoint: str
    ) -> None:
        resp = no_auth_client.get(endpoint)
        assert resp.status_code != 401, (
            f"Expected no 401 on {endpoint!r} in no-auth mode, "
            f"got {resp.status_code}"
        )
