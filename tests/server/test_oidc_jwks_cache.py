"""The OIDC JWKS cache cannot be driven into a fetch per request (review server-10).

Any client could send ``Authorization: Bearer <{"alg":"RS256","kid":"<random>"}>.x.y``:
each unknown ``kid`` ran a blocking ``httpx.get`` against the IdP (10 s timeout)
on the event loop — no negative cache, no minimum refetch interval, and a failed
fetch left the cache stale so the next request fetched again.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from novafabric.server.auth import JwksCache

KEY = {"kid": "known", "kty": "RSA"}


class _Counting:
    """A stand-in for ``JwksCache._fetch`` that counts calls."""

    def __init__(self, *, succeed: bool = True) -> None:
        self.calls = 0
        self.succeed = succeed

    def __call__(self, cache: JwksCache) -> None:
        self.calls += 1
        if self.succeed:
            cache._keys = {"known": KEY}
            cache._fetched_at = time.monotonic()


@pytest.fixture
def counting(monkeypatch: pytest.MonkeyPatch) -> _Counting:
    fetch = _Counting()
    monkeypatch.setattr(JwksCache, "_fetch", lambda self: fetch(self))
    return fetch


def test_a_hundred_unknown_kids_cost_one_fetch(counting: _Counting) -> None:
    cache = JwksCache("https://idp.example.com")
    assert cache.get_key("known") == KEY
    for i in range(100):
        assert cache.get_key(f"random-{i}") is None
    assert counting.calls == 1


def test_a_failing_idp_is_not_retried_on_every_request(counting: _Counting) -> None:
    counting.succeed = False
    cache = JwksCache("https://idp.example.com")
    for i in range(50):
        assert cache.get_key(f"random-{i}") is None
    assert counting.calls == 1


def test_an_unknown_kid_refetches_once_the_interval_has_passed(counting: _Counting) -> None:
    # Key rotation at the IdP must still be picked up.
    cache = JwksCache("https://idp.example.com", min_refetch_seconds=30)
    cache.get_key("known")
    cache._attempted_at -= 31
    cache.get_key("rotated-in")
    assert counting.calls == 2


def test_flush_forces_the_next_fetch(counting: _Counting) -> None:
    cache = JwksCache("https://idp.example.com")
    cache.get_key("known")
    cache.flush()
    assert cache.get_key("known") == KEY
    assert counting.calls == 2


def test_the_fetch_never_runs_on_the_event_loop(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    jwt = pytest.importorskip("jwt")
    from fastapi.testclient import TestClient

    import novafabric.server.auth as auth_mod
    from novafabric.server.app import create_app
    from novafabric.server.config import OidcConfig, ServerConfig

    on_loop: list[bool] = []

    def _fetch(self: JwksCache) -> None:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)

    monkeypatch.setattr(JwksCache, "_fetch", _fetch)
    monkeypatch.setattr(auth_mod, "_jwks_cache", None)
    cfg = ServerConfig(
        db_path=str(tmp_path / "t.db"),
        oidc=OidcConfig(issuer_url="https://idp.example.com", audience="nova"),
    )
    client = TestClient(create_app(cfg), raise_server_exceptions=False)
    forged = jwt.encode({"sub": "x"}, "k" * 32, algorithm="HS256").split(".")
    header = jwt.utils.base64url_encode(b'{"alg":"RS256","kid":"random"}').decode()
    resp = client.get(
        "/v0/assets", headers={"Authorization": f"Bearer {header}.{forged[1]}.{forged[2]}"}
    )
    assert resp.status_code == 401
    assert on_loop == [False]


# ── review server-11: the documented IdPs ────────────────────────────────────


class _FakeResponse:
    def __init__(self, body: dict[str, Any] | None, status: int = 200) -> None:
        self._body, self.status_code = body, status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        assert self._body is not None
        return self._body


def _serve(monkeypatch: pytest.MonkeyPatch, routes: dict[str, Any]) -> list[str]:
    import novafabric.server.auth as auth_mod

    seen: list[str] = []

    def _get(url: str, timeout: float) -> _FakeResponse:
        seen.append(url)
        body = routes.get(url)
        return _FakeResponse(body, 200 if body is not None else 404)

    monkeypatch.setattr(auth_mod.httpx, "get", _get)
    return seen


def test_the_jwks_location_comes_from_the_discovery_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Keycloak publishes /protocol/openid-connect/certs, not /.well-known/jwks.json.
    issuer = "https://kc.example.com/realms/nova"
    certs = f"{issuer}/protocol/openid-connect/certs"
    seen = _serve(monkeypatch, {
        f"{issuer}/.well-known/openid-configuration": {"issuer": issuer, "jwks_uri": certs},
        certs: {"keys": [KEY]},
    })
    assert JwksCache(issuer).get_key("known") == KEY
    assert seen == [f"{issuer}/.well-known/openid-configuration", certs]


def test_without_a_discovery_document_the_conventional_path_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issuer = "https://idp.example.com"
    _serve(monkeypatch, {f"{issuer}/.well-known/jwks.json": {"keys": [KEY]}})
    assert JwksCache(issuer).get_key("known") == KEY


def _oidc_token_client(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, *, configured: str, iss: str
) -> Any:
    import json

    jwt = pytest.importorskip("jwt")
    from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
    from fastapi.testclient import TestClient
    from jwt.algorithms import RSAAlgorithm

    import novafabric.server.auth as auth_mod
    from novafabric.server.app import create_app
    from novafabric.server.config import OidcConfig, ServerConfig

    priv = generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(priv.public_key())) | {"kid": "k1", "alg": "RS256"}
    base = configured.rstrip("/")
    _serve(monkeypatch, {
        f"{base}/.well-known/openid-configuration": {"jwks_uri": f"{base}/keys"},
        f"{base}/keys": {"keys": [jwk]},
    })
    monkeypatch.setattr(auth_mod, "_jwks_cache", None)
    cfg = ServerConfig(
        db_path=str(tmp_path / "t.db"),
        oidc=OidcConfig(issuer_url=configured, audience="nova"),
    )
    now = int(time.time())
    token = jwt.encode(
        {"sub": "u", "aud": "nova", "iss": iss, "iat": now, "exp": now + 600,
         "nova_roles": ["reader"]},
        priv, algorithm="RS256", headers={"kid": "k1"},
    )
    client = TestClient(create_app(cfg), raise_server_exceptions=False)
    return client.get("/v0/assets", headers={"Authorization": f"Bearer {token}"})


def test_an_issuer_with_a_trailing_slash_is_accepted(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Auth0's iss is "https://<tenant>.auth0.com/"; stripping the configured
    # slash made every token an "issuer mismatch".
    resp = _oidc_token_client(
        tmp_path, monkeypatch,
        configured="https://tenant.auth0.com/", iss="https://tenant.auth0.com/",
    )
    assert resp.status_code == 200, resp.text


def test_a_configured_trailing_slash_still_accepts_an_issuer_without_one(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    resp = _oidc_token_client(
        tmp_path, monkeypatch,
        configured="https://kc.example.com/realms/nova/", iss="https://kc.example.com/realms/nova",
    )
    assert resp.status_code == 200, resp.text


def test_a_different_issuer_is_still_refused(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    resp = _oidc_token_client(
        tmp_path, monkeypatch,
        configured="https://tenant.auth0.com/", iss="https://evil.example.com/",
    )
    assert resp.status_code == 401
