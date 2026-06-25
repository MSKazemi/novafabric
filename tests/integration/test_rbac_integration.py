"""RBAC integration tests — each of the four roles tested against
representative endpoints (Track S-5).

Uses OIDC mode with a mock JWKS so no real OIDC provider is needed.
Each role is tested against a representative endpoint to verify the
expected HTTP status code.

Role matrix being tested:
  reader  → GET  /v0/assets        200
  reader  → POST /v0/assets        403
  writer  → POST /v0/assets        200 or 422 (allowed; 422 means invalid spec)
  admin   → POST /v0/admin/flush-jwks  200
  auditor → GET  /v0/evidence/{id} 404 (allowed; 404 means bundle not found)
  auditor → POST /v0/assets        403
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

import jwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.auth import JwksCache  # noqa: E402
from novafabric.server.config import OidcConfig, ServerConfig  # noqa: E402

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_ISSUER = "https://oidc.example.com"
_AUDIENCE = "nova-server"
_KID = "integ-rbac-kid"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rsa_private_key() -> Any:
    return generate_private_key(public_exponent=65537, key_size=2048)


def _rsa_jwk(priv_key: Any) -> dict[str, Any]:
    from jwt.algorithms import RSAAlgorithm

    pub_key = priv_key.public_key()
    jwk_dict: dict[str, Any] = json.loads(RSAAlgorithm.to_jwk(pub_key))
    jwk_dict["kid"] = _KID
    jwk_dict["alg"] = "RS256"
    jwk_dict["use"] = "sig"
    return jwk_dict


def _make_token(priv_key: Any, roles: list[str]) -> str:
    now = int(time.time())
    payload = {
        "sub": "integ@example.com",
        "aud": _AUDIENCE,
        "iss": _ISSUER,
        "iat": now,
        "exp": now + 3600,
        "nova_roles": roles,
    }
    return jwt.encode(payload, priv_key, algorithm="RS256", headers={"kid": _KID})


def _auth_headers(priv_key: Any, roles: list[str]) -> dict[str, str]:
    token = _make_token(priv_key, roles)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Session-scoped RSA keypair (generated once per test session)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_key() -> Any:
    return _rsa_private_key()


@pytest.fixture(scope="module")
def jwk(rsa_key: Any) -> dict[str, Any]:
    return _rsa_jwk(rsa_key)


# ---------------------------------------------------------------------------
# OIDC-enabled TestClient fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def rbac_client(tmp_path: Path, jwk: dict[str, Any]) -> TestClient:
    """Create a TestClient with OIDC enabled and JWKS populated from the test keypair."""
    cfg = ServerConfig(
        db_path=str(tmp_path / "rbac-integ.db"),
        oidc=OidcConfig(issuer_url=_ISSUER, audience=_AUDIENCE),
    )

    # Reset the global JWKS cache so each test gets a clean state
    import novafabric.server.auth as auth_mod

    auth_mod._jwks_cache = None

    captured_jwk = dict(jwk)

    def _fake_fetch(self: JwksCache) -> None:
        self._keys = {captured_jwk["kid"]: captured_jwk}
        self._fetched_at = time.monotonic()

    with patch.object(JwksCache, "_fetch", _fake_fetch):
        app = create_app(cfg)
        return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Reader role
# ---------------------------------------------------------------------------


class TestReaderRole:
    def test_reader_can_list_assets(
        self, rbac_client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        def _fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", _fake_fetch):
            resp = rbac_client.get(
                "/v0/assets", headers=_auth_headers(rsa_key, ["reader"])
            )
        assert resp.status_code == 200

    def test_reader_cannot_create_asset(
        self, rbac_client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        def _fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", _fake_fetch):
            resp = rbac_client.post(
                "/v0/assets",
                json={"spec_yaml": "name: forbidden-model"},
                headers=_auth_headers(rsa_key, ["reader"]),
            )
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"]["code"] == "forbidden"

    def test_reader_can_list_capsules(
        self, rbac_client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        def _fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", _fake_fetch):
            resp = rbac_client.get(
                "/v0/capsules", headers=_auth_headers(rsa_key, ["reader"])
            )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Writer role
# ---------------------------------------------------------------------------


class TestWriterRole:
    def test_writer_can_create_asset(
        self, rbac_client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        """Writer is allowed on POST /v0/assets.

        Returns 201 (success), 422 (invalid spec), or 409 (duplicate) — all
        indicate the auth check passed.  403 would indicate the role was rejected.
        """
        spec_yaml = """\
novafabric_spec_version: "0.1"
asset_type: model
name: rbac-writer-model
version: "1.0.0"
status: development
spec:
  framework: pytorch
  artifact_path: /tmp/rbac-model.pt
"""

        def _fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", _fake_fetch):
            resp = rbac_client.post(
                "/v0/assets",
                json={"spec_yaml": spec_yaml},
                headers=_auth_headers(rsa_key, ["writer"]),
            )
        assert resp.status_code != 403, (
            f"Writer should be allowed on POST /v0/assets, got 403: {resp.json()}"
        )
        assert resp.status_code in (201, 409, 422), (
            f"Unexpected status {resp.status_code}: {resp.text}"
        )

    def test_writer_can_list_assets(
        self, rbac_client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        def _fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", _fake_fetch):
            resp = rbac_client.get(
                "/v0/assets", headers=_auth_headers(rsa_key, ["writer"])
            )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Admin role
# ---------------------------------------------------------------------------


class TestAdminRole:
    def test_admin_can_flush_jwks(
        self, rbac_client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        def _fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", _fake_fetch):
            resp = rbac_client.post(
                "/v0/admin/flush-jwks", headers=_auth_headers(rsa_key, ["admin"])
            )
        assert resp.status_code == 200

    def test_admin_can_list_assets(
        self, rbac_client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        def _fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", _fake_fetch):
            resp = rbac_client.get(
                "/v0/assets", headers=_auth_headers(rsa_key, ["admin"])
            )
        assert resp.status_code == 200

    def test_admin_can_create_asset(
        self, rbac_client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        spec_yaml = """\
novafabric_spec_version: "0.1"
asset_type: model
name: rbac-admin-model
version: "1.0.0"
status: development
spec:
  framework: pytorch
  artifact_path: /tmp/admin-model.pt
"""

        def _fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", _fake_fetch):
            resp = rbac_client.post(
                "/v0/assets",
                json={"spec_yaml": spec_yaml},
                headers=_auth_headers(rsa_key, ["admin"]),
            )
        # admin satisfies writer requirement → not 403
        assert resp.status_code != 403


# ---------------------------------------------------------------------------
# Auditor role
# ---------------------------------------------------------------------------


class TestAuditorRole:
    def test_auditor_can_read_evidence_not_found(
        self, rbac_client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        """Auditor has access to GET /v0/evidence/{id}.

        404 means the bundle was not found (auth passed); 403 would mean denied.
        """

        def _fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", _fake_fetch):
            resp = rbac_client.get(
                "/v0/evidence/no-such-bundle",
                headers=_auth_headers(rsa_key, ["auditor"]),
            )
        assert resp.status_code in (200, 404), (
            f"Auditor should be allowed on GET /v0/evidence/{{id}}, "
            f"got {resp.status_code}"
        )

    def test_auditor_cannot_create_asset(
        self, rbac_client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        def _fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", _fake_fetch):
            resp = rbac_client.post(
                "/v0/assets",
                json={"spec_yaml": "name: auditor-asset"},
                headers=_auth_headers(rsa_key, ["auditor"]),
            )
        assert resp.status_code == 403

    def test_auditor_cannot_list_assets(
        self, rbac_client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        """Auditor is orthogonal to reader — cannot list assets."""

        def _fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", _fake_fetch):
            resp = rbac_client.get(
                "/v0/assets", headers=_auth_headers(rsa_key, ["auditor"])
            )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# No token → 401
# ---------------------------------------------------------------------------


class TestMissingToken:
    def test_no_token_returns_401(
        self, rbac_client: TestClient, jwk: dict[str, Any]
    ) -> None:
        def _fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", _fake_fetch):
            resp = rbac_client.get("/v0/assets")
        assert resp.status_code == 401

    def test_malformed_token_returns_401(
        self, rbac_client: TestClient, jwk: dict[str, Any]
    ) -> None:
        def _fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", _fake_fetch):
            resp = rbac_client.get(
                "/v0/assets",
                headers={"Authorization": "Bearer not.a.valid.jwt"},
            )
        assert resp.status_code == 401
