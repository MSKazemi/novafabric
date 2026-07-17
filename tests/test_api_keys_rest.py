"""Tests for the /v0/api-keys REST resource (ADR-0193 Track 1, slice 2).

Admin-gated CRUD over first-class API keys, mirroring the service-accounts
resource pattern:

- POST   /v0/api-keys              — create (returns the key ONCE)
- GET    /v0/api-keys              — list metadata (never secrets/hashes)
- DELETE /v0/api-keys/{key_id}     — revoke (immediate)
- POST   /v0/api-keys/{key_id}/rotate — rotate (returns successor ONCE)

Auth is exercised end-to-end through the existing RBAC path: the caller
presents an ``Authorization: Bearer nvfk_...`` admin key; a reader key is
rejected with 403.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server.api_keys import create_key, parse_key, verify_key  # noqa: E402
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import ServerConfig  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp_audit_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "audit.jsonl"
    from novafabric.audit import _paths

    monkeypatch.setattr(_paths, "AUDIT_LOG_PATH", path)
    return path


# The server's auth layer resolves API keys from the default registry
# (get_db_path(), pointed at a per-test NOVAFABRIC_HOME by the hermetic
# conftest fixture). Leave config.db_path unset so the /v0/api-keys routes use
# that same store — mirroring the slice-1 TestAuthIntegration harness.


@pytest.fixture
def client() -> TestClient:
    cfg = ServerConfig(local_token="unused-local-token")
    return TestClient(create_app(cfg), raise_server_exceptions=False)


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def admin_key() -> str:
    key, _ = create_key("admin@x", ["admin"], actor="seed")
    return key


@pytest.fixture
def reader_key() -> str:
    key, _ = create_key("reader@x", ["reader"], actor="seed")
    return key


class TestCreate:
    def test_admin_creates_key_returned_once(
        self, client: TestClient, admin_key: str
    ) -> None:
        resp = client.post(
            "/v0/api-keys",
            json={"owner": "svc:ci", "roles": ["reader", "writer"]},
            headers=_bearer(admin_key),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "key" in body
        assert body["key"].startswith("nvfk_")
        # the returned key actually works and carries the requested roles
        ctx = verify_key(body["key"])
        assert ctx is not None
        assert ctx.subject == "svc:ci"
        assert sorted(ctx.roles) == ["reader", "writer"]

    def test_reader_forbidden(
        self, client: TestClient, reader_key: str
    ) -> None:
        resp = client.post(
            "/v0/api-keys",
            json={"owner": "svc:ci", "roles": ["reader"]},
            headers=_bearer(reader_key),
        )
        assert resp.status_code == 403, resp.text

    def test_unauthenticated_401(self, client: TestClient) -> None:
        resp = client.post("/v0/api-keys", json={"owner": "x", "roles": ["reader"]})
        assert resp.status_code == 401, resp.text

    def test_bad_role_is_400_envelope(
        self, client: TestClient, admin_key: str
    ) -> None:
        resp = client.post(
            "/v0/api-keys",
            json={"owner": "svc:ci", "roles": ["root"]},
            headers=_bearer(admin_key),
        )
        assert resp.status_code == 400, resp.text
        assert "error" in resp.json()


class TestList:
    def test_list_metadata_no_secrets(
        self, client: TestClient, admin_key: str
    ) -> None:
        key, _ = create_key("alice@x", ["reader"], actor="seed")
        _, secret = parse_key(key)  # type: ignore[misc]
        resp = client.get("/v0/api-keys", headers=_bearer(admin_key))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "api_keys" in body
        dumped = resp.text
        assert secret not in dumped
        assert "secret_sha256" not in dumped
        assert hashlib.sha256(secret.encode()).hexdigest() not in dumped

    def test_reader_forbidden(self, client: TestClient, reader_key: str) -> None:
        assert client.get(
            "/v0/api-keys", headers=_bearer(reader_key)
        ).status_code == 403


class TestRevoke:
    def test_admin_revokes(
        self, client: TestClient, admin_key: str
    ) -> None:
        key, record = create_key("alice@x", ["reader"], actor="seed")
        assert verify_key(key) is not None
        resp = client.delete(
            f"/v0/api-keys/{record['key_id']}", headers=_bearer(admin_key)
        )
        assert resp.status_code == 200, resp.text
        assert verify_key(key) is None

    def test_revoke_unknown_404(
        self, client: TestClient, admin_key: str
    ) -> None:
        resp = client.delete("/v0/api-keys/nosuchid", headers=_bearer(admin_key))
        assert resp.status_code == 404, resp.text
        assert "error" in resp.json()

    def test_reader_forbidden(
        self, client: TestClient, reader_key: str
    ) -> None:
        _, record = create_key("alice@x", ["reader"], actor="seed")
        resp = client.delete(
            f"/v0/api-keys/{record['key_id']}", headers=_bearer(reader_key)
        )
        assert resp.status_code == 403, resp.text


class TestRotate:
    def test_admin_rotates_returns_successor_once(
        self, client: TestClient, admin_key: str
    ) -> None:
        _, record = create_key("svc:ci", ["reader"], actor="seed")
        resp = client.post(
            f"/v0/api-keys/{record['key_id']}/rotate", headers=_bearer(admin_key)
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["key"].startswith("nvfk_")
        assert "overlap_seconds" in body
        ctx = verify_key(body["key"])
        assert ctx is not None
        assert ctx.subject == "svc:ci"

    def test_rotate_unknown_404(
        self, client: TestClient, admin_key: str
    ) -> None:
        resp = client.post(
            "/v0/api-keys/nosuchid/rotate", headers=_bearer(admin_key)
        )
        assert resp.status_code == 404, resp.text

    def test_reader_forbidden(
        self, client: TestClient, reader_key: str
    ) -> None:
        _, record = create_key("svc:ci", ["reader"], actor="seed")
        resp = client.post(
            f"/v0/api-keys/{record['key_id']}/rotate", headers=_bearer(reader_key)
        )
        assert resp.status_code == 403, resp.text
