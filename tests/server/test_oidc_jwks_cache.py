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
