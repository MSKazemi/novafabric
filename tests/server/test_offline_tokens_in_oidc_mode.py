"""Offline (service-account) tokens authenticate in OIDC mode too (review server-15).

``POST /v0/service-accounts`` mints an offline EdDSA token whenever
``NOVAFABRIC_OFFLINE_KEY_PATH`` is set, but ``verify_token`` tried offline tokens
only when OIDC was off: with OIDC on, creation answered 201 and every use of the
token was 401 "Unknown key ID".
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import novafabric.server.auth as auth_mod
from novafabric.server.app import create_app
from novafabric.server.auth import JwksCache
from novafabric.server.config import OidcConfig, ServerConfig
from novafabric.server.offline_tokens import generate_keypair, issue_token


@pytest.fixture
def jwks_fetches(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(JwksCache, "_fetch", lambda self: calls.append(1))
    monkeypatch.setattr(auth_mod, "_jwks_cache", None)
    return calls


def _client(tmp_path: Path, *, offline_key: Path | None) -> TestClient:
    cfg = ServerConfig(
        db_path=str(tmp_path / "t.db"),
        oidc=OidcConfig(issuer_url="https://idp.example.com", audience="nova"),
        offline_key_path=str(offline_key) if offline_key else None,
    )
    return TestClient(create_app(cfg), raise_server_exceptions=False)


def _keypair(tmp_path: Path, name: str) -> Path:
    key = tmp_path / name / "offline-key.pem"
    generate_keypair(key)
    return key


def test_an_offline_token_authenticates_with_oidc_enabled(
    tmp_path: Path, jwks_fetches: list[int]
) -> None:
    key = _keypair(tmp_path, "keys")
    token = issue_token("ci-bot", ["reader"], 1, key)
    resp = _client(tmp_path, offline_key=key).get(
        "/v0/assets", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    assert jwks_fetches == []  # never sent to the IdP's key set


def test_an_offline_token_signed_by_another_key_is_refused(
    tmp_path: Path, jwks_fetches: list[int]
) -> None:
    key = _keypair(tmp_path, "keys")
    other = _keypair(tmp_path, "other")
    forged = issue_token("ci-bot", ["admin"], 1, other)
    resp = _client(tmp_path, offline_key=key).get(
        "/v0/assets", headers={"Authorization": f"Bearer {forged}"}
    )
    assert resp.status_code == 401


def test_without_an_offline_key_an_offline_token_is_refused(
    tmp_path: Path, jwks_fetches: list[int]
) -> None:
    key = _keypair(tmp_path, "keys")
    token = issue_token("ci-bot", ["reader"], 1, key)
    resp = _client(tmp_path, offline_key=None).get(
        "/v0/assets", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401
