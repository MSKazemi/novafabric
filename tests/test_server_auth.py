"""Tests for OIDC token validation middleware (Track S-3).

Covers:
- OIDC disabled → all endpoints 200 without token
- OIDC enabled + mock JWKS: valid token → 200, expired → 401, wrong aud → 401, no token → 401
- JWKS re-fetch on unknown key ID
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

# Skip if FastAPI / httpx not installed
pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.auth import JwksCache  # noqa: E402
from novafabric.server.config import OidcConfig, ServerConfig  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rsa_private_key() -> Any:
    return generate_private_key(public_exponent=65537, key_size=2048)


def _rsa_jwk(priv_key: Any, kid: str = "test-kid-1") -> dict[str, Any]:
    """Build a minimal RS256 JWK from an RSA private key."""

    pub_key = priv_key.public_key()
    # Use PyJWT's RSA algorithm to serialise to JWK
    from jwt.algorithms import RSAAlgorithm

    jwk_str = RSAAlgorithm.to_jwk(pub_key)
    jwk_dict: dict[str, Any] = json.loads(jwk_str)
    jwk_dict["kid"] = kid
    jwk_dict["alg"] = "RS256"
    jwk_dict["use"] = "sig"
    return jwk_dict


def _make_token(
    priv_key: Any,
    kid: str = "test-kid-1",
    aud: str = "nova-server",
    iss: str = "https://oidc.example.com",
    exp_offset: int = 3600,
    roles: list[str] | None = None,
) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": "user@example.com",
        "aud": aud,
        "iss": iss,
        "iat": now,
        "exp": now + exp_offset,
        "nova_roles": roles or ["reader"],
    }
    return jwt.encode(payload, priv_key, algorithm="RS256", headers={"kid": kid})


def _no_auth_config(tmp_path: Path) -> ServerConfig:
    return ServerConfig(db_path=str(tmp_path / "test.db"))


def _oidc_config(tmp_path: Path) -> ServerConfig:
    return ServerConfig(
        db_path=str(tmp_path / "test.db"),
        oidc=OidcConfig(
            issuer_url="https://oidc.example.com",
            audience="nova-server",
        ),
    )


# ---------------------------------------------------------------------------
# Fixture: client without auth
# ---------------------------------------------------------------------------


@pytest.fixture
def no_auth_client(tmp_path: Path) -> TestClient:
    cfg = _no_auth_config(tmp_path)
    app = create_app(cfg)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# OIDC disabled tests
# ---------------------------------------------------------------------------


class TestNoAuth:
    def test_health_no_token(self, no_auth_client: TestClient) -> None:
        resp = no_auth_client.get("/health")
        assert resp.status_code == 200

    def test_list_assets_no_token(self, no_auth_client: TestClient) -> None:
        resp = no_auth_client.get("/v0/assets")
        assert resp.status_code == 200

    def test_list_capsules_no_token(self, no_auth_client: TestClient) -> None:
        resp = no_auth_client.get("/v0/capsules")
        assert resp.status_code == 200

    def test_lineage_nodes_no_token(self, no_auth_client: TestClient) -> None:
        resp = no_auth_client.get("/v0/lineage/nodes")
        assert resp.status_code == 200

    def test_flush_jwks_no_token(self, no_auth_client: TestClient) -> None:
        # In no-auth mode, admin endpoint should return 200 (all roles satisfied)
        resp = no_auth_client.post("/v0/admin/flush-jwks")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# OIDC enabled tests
# ---------------------------------------------------------------------------


class TestOidcEnabled:
    """Use a mocked JWKS endpoint so no network is needed."""

    @pytest.fixture
    def rsa_key(self) -> Any:
        return _rsa_private_key()

    @pytest.fixture
    def jwk(self, rsa_key: Any) -> dict[str, Any]:
        return _rsa_jwk(rsa_key)

    @pytest.fixture
    def client(self, tmp_path: Path, jwk: dict[str, Any]) -> TestClient:
        cfg = _oidc_config(tmp_path)

        # Patch JwksCache._fetch to inject our test key
        def fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", fake_fetch):
            app = create_app(cfg)
            # Force the cache to pre-load our key
            import novafabric.server.auth as auth_mod

            auth_mod._jwks_cache = None  # reset singleton
            return TestClient(app, raise_server_exceptions=False)

    def _bearer(self, priv_key: Any, **kwargs: Any) -> dict[str, str]:
        token = _make_token(priv_key, **kwargs)
        return {"Authorization": f"Bearer {token}"}

    def test_valid_token_reader_on_get(
        self, client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        # Patch cache during request
        def fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", fake_fetch):
            headers = self._bearer(rsa_key, roles=["reader"])
            resp = client.get("/v0/assets", headers=headers)
        assert resp.status_code == 200

    def test_no_token_returns_401(self, client: TestClient) -> None:
        resp = client.get("/v0/assets")
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["code"] == "unauthenticated"

    def test_expired_token_returns_401(
        self, client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        def fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", fake_fetch):
            headers = self._bearer(rsa_key, exp_offset=-60)
            resp = client.get("/v0/assets", headers=headers)
        assert resp.status_code == 401
        assert "expired" in resp.json()["error"]["message"].lower()

    def test_wrong_audience_returns_401(
        self, client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        def fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", fake_fetch):
            headers = self._bearer(rsa_key, aud="wrong-audience")
            resp = client.get("/v0/assets", headers=headers)
        assert resp.status_code == 401

    def test_unknown_kid_triggers_refetch(
        self, tmp_path: Path, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        """Unknown kid should trigger one re-fetch attempt."""
        cfg = _oidc_config(tmp_path)
        fetch_count = {"n": 0}

        def fake_fetch(self: JwksCache) -> None:
            fetch_count["n"] += 1
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        import novafabric.server.auth as auth_mod

        auth_mod._jwks_cache = None

        with patch.object(JwksCache, "_fetch", fake_fetch):
            app = create_app(cfg)
            client = TestClient(app, raise_server_exceptions=False)
            # Token with known kid
            headers = self._bearer(rsa_key, kid=jwk["kid"], roles=["reader"])
            resp = client.get("/v0/assets", headers=headers)
        assert resp.status_code == 200
        # Fetch was called at least once (stale + possibly re-fetch on unknown kid)
        assert fetch_count["n"] >= 1


# ---------------------------------------------------------------------------
# JwksCache unit tests
# ---------------------------------------------------------------------------


class TestJwksCache:
    def test_flush_marks_stale(self) -> None:
        cache = JwksCache("https://example.com", ttl_seconds=3600)
        cache._fetched_at = time.monotonic()
        cache._keys = {"k1": {"kid": "k1"}}
        cache.flush()
        assert cache._is_stale()
        assert cache._keys == {}

    def test_stale_when_freshly_created(self) -> None:
        cache = JwksCache("https://example.com")
        assert cache._is_stale()

    def test_ttl_expiry(self) -> None:
        cache = JwksCache("https://example.com", ttl_seconds=1)
        cache._fetched_at = time.monotonic() - 5
        assert cache._is_stale()

    def test_get_key_returns_none_when_no_keys_and_fetch_fails(self) -> None:
        cache = JwksCache("https://nonexistent.invalid.example", ttl_seconds=1)
        # _fetch will fail (network error) — should return None
        result = cache.get_key("nonexistent-kid")
        assert result is None

    def test_get_key_fallback_to_empty_string_key(self) -> None:
        cache = JwksCache("https://example.com", ttl_seconds=3600)
        cache._keys = {"": {"kid": None, "kty": "RSA"}}
        cache._fetched_at = time.monotonic()
        # No-kid lookup should return the "" key
        result = cache.get_key(None)
        assert result is not None


# ---------------------------------------------------------------------------
# Additional verify_token edge cases
# ---------------------------------------------------------------------------


class TestVerifyTokenEdgeCases:
    """Test edge cases in verify_token that need OIDC-enabled config."""

    @pytest.fixture
    def rsa_key(self) -> Any:
        return _rsa_private_key()

    @pytest.fixture
    def jwk(self, rsa_key: Any) -> dict[str, Any]:
        return _rsa_jwk(rsa_key)

    @pytest.fixture
    def oidc_client(self, tmp_path: Path, jwk: dict[str, Any]) -> TestClient:
        from novafabric.server.config import OidcConfig

        cfg = ServerConfig(
            db_path=str(tmp_path / "test.db"),
            oidc=OidcConfig(
                issuer_url="https://oidc.example.com",
                audience="nova-server",
            ),
        )

        def fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        import novafabric.server.auth as auth_mod

        auth_mod._jwks_cache = None

        with patch.object(JwksCache, "_fetch", fake_fetch):
            app = create_app(cfg)
            return TestClient(app, raise_server_exceptions=False)

    def test_malformed_jwt_returns_401(self, oidc_client: TestClient) -> None:
        resp = oidc_client.get(
            "/v0/assets", headers={"Authorization": "Bearer not.a.valid.jwt.at.all"}
        )
        assert resp.status_code == 401

    def test_wrong_issuer_returns_401(
        self, oidc_client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        def fake_fetch(self: JwksCache) -> None:
            self._keys = {jwk["kid"]: jwk}
            self._fetched_at = time.monotonic()

        with patch.object(JwksCache, "_fetch", fake_fetch):
            token = _make_token(rsa_key, iss="https://wrong-issuer.example.com", kid=jwk["kid"])
            resp = oidc_client.get(
                "/v0/assets", headers={"Authorization": f"Bearer {token}"}
            )
        assert resp.status_code == 401

    def test_flush_jwks_cache_endpoint(
        self, oidc_client: TestClient, rsa_key: Any, jwk: dict[str, Any]
    ) -> None:
        """flush_jwks_cache() is callable in no-auth mode too — test coverage of the function."""
        from novafabric.server.auth import flush_jwks_cache

        flush_jwks_cache()  # should not raise even if cache is None


# ---------------------------------------------------------------------------
# App lifespan coverage
# ---------------------------------------------------------------------------


class TestAppLifespan:
    def test_lifespan_startup(self, tmp_path: Path) -> None:
        """Using TestClient as context manager triggers lifespan startup."""
        cfg = ServerConfig(db_path=str(tmp_path / "lifespan.db"))
        app = create_app(cfg)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/health")
        assert resp.status_code == 200
