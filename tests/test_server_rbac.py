"""Tests for RBAC enforcement (Track S-3).

Covers role hierarchy, endpoint permission map, 401 vs 403 distinction.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import jwt
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.auth import JwksCache  # noqa: E402
from novafabric.server.config import OidcConfig, ServerConfig  # noqa: E402
from novafabric.server.rbac import Role, _satisfies  # noqa: E402

# ---------------------------------------------------------------------------
# RBAC logic unit tests
# ---------------------------------------------------------------------------


class TestRbacLogic:
    def test_admin_satisfies_all(self) -> None:
        assert _satisfies(["admin"], Role.reader) is True
        assert _satisfies(["admin"], Role.writer) is True
        assert _satisfies(["admin"], Role.admin) is True
        assert _satisfies(["admin"], Role.auditor) is True

    def test_reader_satisfies_only_reader(self) -> None:
        assert _satisfies(["reader"], Role.reader) is True
        assert _satisfies(["reader"], Role.writer) is False
        assert _satisfies(["reader"], Role.admin) is False

    def test_writer_satisfies_reader_and_writer(self) -> None:
        assert _satisfies(["writer"], Role.reader) is True
        assert _satisfies(["writer"], Role.writer) is True
        assert _satisfies(["writer"], Role.admin) is False

    def test_auditor_is_orthogonal(self) -> None:
        assert _satisfies(["auditor"], Role.auditor) is True
        assert _satisfies(["auditor"], Role.reader) is False
        assert _satisfies(["auditor"], Role.writer) is False

    def test_empty_roles_fails(self) -> None:
        assert _satisfies([], Role.reader) is False

    def test_multiple_roles(self) -> None:
        assert _satisfies(["reader", "auditor"], Role.auditor) is True
        assert _satisfies(["reader", "writer"], Role.writer) is True


# ---------------------------------------------------------------------------
# Helpers: OIDC-enabled app + token factory
# ---------------------------------------------------------------------------


def _rsa_private_key() -> Any:
    from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

    return generate_private_key(public_exponent=65537, key_size=2048)


def _rsa_jwk(priv_key: Any, kid: str = "rbac-kid") -> dict[str, Any]:
    import json

    from jwt.algorithms import RSAAlgorithm

    pub_key = priv_key.public_key()
    jwk_dict: dict[str, Any] = json.loads(RSAAlgorithm.to_jwk(pub_key))
    jwk_dict["kid"] = kid
    return jwk_dict


def _make_token(priv_key: Any, roles: list[str], kid: str = "rbac-kid") -> str:
    now = int(time.time())
    payload = {
        "sub": "user@example.com",
        "aud": "nova-server",
        "iss": "https://oidc.example.com",
        "iat": now,
        "exp": now + 3600,
        "nova_roles": roles,
    }
    return jwt.encode(payload, priv_key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture(scope="module")
def rsa_key() -> Any:
    return _rsa_private_key()


@pytest.fixture(scope="module")
def jwk(rsa_key: Any) -> dict[str, Any]:
    return _rsa_jwk(rsa_key)


@pytest.fixture
def oidc_client(tmp_path: Path, jwk: dict[str, Any]) -> TestClient:
    cfg = ServerConfig(
        db_path=str(tmp_path / "rbac.db"),
        oidc=OidcConfig(issuer_url="https://oidc.example.com", audience="nova-server"),
    )

    def fake_fetch(self: JwksCache) -> None:
        self._keys = {jwk["kid"]: jwk}
        self._fetched_at = time.monotonic()

    import novafabric.server.auth as auth_mod

    auth_mod._jwks_cache = None

    with patch.object(JwksCache, "_fetch", fake_fetch):
        app = create_app(cfg)
        return TestClient(app, raise_server_exceptions=False)


def _hdrs(priv_key: Any, roles: list[str], jwk: dict[str, Any]) -> dict[str, str]:
    token = _make_token(priv_key, roles, kid=jwk["kid"])
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Endpoint permission tests
# ---------------------------------------------------------------------------


class TestEndpointPermissions:
    def test_reader_can_list_assets(
        self, oidc_client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        def fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", fake_fetch):
            resp = oidc_client.get("/v0/assets", headers=_hdrs(rsa_key, ["reader"], jwk))
        assert resp.status_code == 200

    def test_reader_cannot_create_asset(
        self, oidc_client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        def fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", fake_fetch):
            resp = oidc_client.post(
                "/v0/assets",
                json={"spec_yaml": "x: 1"},
                headers=_hdrs(rsa_key, ["reader"], jwk),
            )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden"

    def test_writer_can_create_asset(
        self, oidc_client: TestClient, rsa_key: Any, jwk: dict[str, Any], tmp_path: Path
    ) -> None:
        """Writer role allowed on POST /v0/assets (returns 422 for bad spec, not 403)."""

        def fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", fake_fetch):
            resp = oidc_client.post(
                "/v0/assets",
                json={"spec_yaml": "bad: yaml: !!"},
                headers=_hdrs(rsa_key, ["writer"], jwk),
            )
        # Auth passed → error is spec validation, not RBAC
        assert resp.status_code in (400, 422), resp.json()
        assert resp.json()["error"]["code"] != "forbidden"

    def test_admin_can_flush_jwks(
        self, oidc_client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        def fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", fake_fetch):
            resp = oidc_client.post(
                "/v0/admin/flush-jwks", headers=_hdrs(rsa_key, ["admin"], jwk)
            )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_reader_cannot_flush_jwks(
        self, oidc_client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        def fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", fake_fetch):
            resp = oidc_client.post(
                "/v0/admin/flush-jwks", headers=_hdrs(rsa_key, ["reader"], jwk)
            )
        assert resp.status_code == 403

    def test_no_token_returns_401_not_403(self, oidc_client: TestClient) -> None:
        resp = oidc_client.get("/v0/assets")
        assert resp.status_code == 401

    def test_auditor_can_view_evidence(
        self, oidc_client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        """GET /v0/evidence/{id} requires auditor role — returns 404 (not 403) with auditor."""

        def fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", fake_fetch):
            resp = oidc_client.get(
                "/v0/evidence/nonexistent-id",
                headers=_hdrs(rsa_key, ["auditor"], jwk),
            )
        # Auth passed → 404 from evidence handler (not 403)
        assert resp.status_code == 404

    def test_reader_cannot_view_evidence(
        self, oidc_client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        """GET /v0/evidence/{id} requires auditor — reader gets 403."""

        def fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", fake_fetch):
            resp = oidc_client.get(
                "/v0/evidence/nonexistent-id",
                headers=_hdrs(rsa_key, ["reader"], jwk),
            )
        assert resp.status_code == 403

    def test_reader_cannot_post_evidence(
        self, oidc_client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        def fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", fake_fetch):
            resp = oidc_client.post(
                "/v0/evidence",
                json={"run_id": "fake-run"},
                headers=_hdrs(rsa_key, ["reader"], jwk),
            )
        assert resp.status_code == 403

    def test_writer_can_post_evidence(
        self, oidc_client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        """Writer on POST /v0/evidence → 404 (no capsule), not 403."""

        def fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", fake_fetch):
            resp = oidc_client.post(
                "/v0/evidence",
                json={"run_id": "fake-run"},
                headers=_hdrs(rsa_key, ["writer"], jwk),
            )
        assert resp.status_code in (404, 400, 422)
        assert resp.json()["error"]["code"] != "forbidden"


# ---------------------------------------------------------------------------
# DA-1 / ADR-0060: /v0/admin/roles RBAC + integration
# ---------------------------------------------------------------------------


class TestAdminRolesRBAC:
    def test_admin_can_list_roles(
        self, oidc_client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        def fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", fake_fetch):
            resp = oidc_client.get(
                "/v0/admin/roles", headers=_hdrs(rsa_key, ["admin"], jwk)
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "roles" in body
        assert "server_mode" in body

    def test_reader_cannot_list_roles(
        self, oidc_client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        def fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", fake_fetch):
            resp = oidc_client.get(
                "/v0/admin/roles", headers=_hdrs(rsa_key, ["reader"], jwk)
            )
        assert resp.status_code == 403

    def test_writer_cannot_assign_role(
        self, oidc_client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        def fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", fake_fetch):
            resp = oidc_client.post(
                "/v0/admin/roles",
                json={"subject": "alice@example.com", "role": "writer"},
                headers=_hdrs(rsa_key, ["writer"], jwk),
            )
        assert resp.status_code == 403

    def test_admin_can_assign_role(
        self,
        oidc_client: TestClient,
        rsa_key: Any,
        jwk: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Redirect audit log so test doesn't pollute the real one
        monkeypatch.setenv(
            "NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(tmp_path / "audit.jsonl")
        )

        def fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", fake_fetch):
            resp = oidc_client.post(
                "/v0/admin/roles",
                json={"subject": "alice@example.com", "role": "writer"},
                headers=_hdrs(rsa_key, ["admin"], jwk),
            )
        assert resp.status_code == 201, resp.json()
        assert resp.json()["ok"] is True

    def test_revoke_missing_returns_404(
        self,
        oidc_client: TestClient,
        rsa_key: Any,
        jwk: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(tmp_path / "audit.jsonl")
        )

        def fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", fake_fetch):
            resp = oidc_client.delete(
                "/v0/admin/roles/ghost@example.com/reader",
                headers=_hdrs(rsa_key, ["admin"], jwk),
            )
        assert resp.status_code == 404

    def test_no_token_returns_401(self, oidc_client: TestClient) -> None:
        resp = oidc_client.get("/v0/admin/roles")
        assert resp.status_code == 401

    def test_admin_can_revoke_role(
        self,
        oidc_client: TestClient,
        rsa_key: Any,
        jwk: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(tmp_path / "audit.jsonl")
        )

        def fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", fake_fetch):
            assign = oidc_client.post(
                "/v0/admin/roles",
                json={"subject": "bob@example.com", "role": "writer"},
                headers=_hdrs(rsa_key, ["admin"], jwk),
            )
            assert assign.status_code == 201, assign.json()
            revoke = oidc_client.delete(
                "/v0/admin/roles/bob@example.com/writer",
                headers=_hdrs(rsa_key, ["admin"], jwk),
            )
        assert revoke.status_code == 200, revoke.json()
        body = revoke.json()
        assert body["ok"] is True
        assert body["subject"] == "bob@example.com"
        assert body["role"] == "writer"

    def test_revoke_last_admin_returns_409(
        self,
        oidc_client: TestClient,
        rsa_key: Any,
        jwk: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Ensure the lockout invariant fires: clear NOVA_OIDC_ISSUER so the
        # rbac_store guard considers this a no-OIDC environment.
        monkeypatch.delenv("NOVA_OIDC_ISSUER", raising=False)
        monkeypatch.setenv(
            "NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(tmp_path / "audit.jsonl")
        )

        def fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", fake_fetch):
            oidc_client.post(
                "/v0/admin/roles",
                json={"subject": "solo@example.com", "role": "admin"},
                headers=_hdrs(rsa_key, ["admin"], jwk),
            )
            resp = oidc_client.delete(
                "/v0/admin/roles/solo@example.com/admin",
                headers=_hdrs(rsa_key, ["admin"], jwk),
            )
        assert resp.status_code == 409
        assert "last admin" in resp.json()["detail"].lower()
