"""Tests for Device Authorization Grant routes (/v0/auth/*) (Track S-3)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import ServerConfig  # noqa: E402


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    # The device-grant demo flow is opt-in (default off); enable it here since
    # these tests exercise it directly.
    cfg = ServerConfig(db_path=str(tmp_path / "test.db"), demo_device_grant=True)
    app = create_app(cfg)
    return TestClient(app, raise_server_exceptions=False)


def test_demo_device_grant_disabled_by_default(tmp_path: Path) -> None:
    """With the default config the device-grant endpoints are not mounted."""
    cfg = ServerConfig(db_path=str(tmp_path / "test.db"))
    app = create_app(cfg)
    disabled = TestClient(app, raise_server_exceptions=False)
    assert disabled.post("/v0/auth/device/code", json={}).status_code == 404
    assert disabled.post("/v0/auth/approve", json={"user_code": "X"}).status_code == 404


def test_approve_rejects_unknown_role(client: TestClient) -> None:
    """Caller-supplied roles are validated against the RBAC allowlist."""
    code_resp = client.post("/v0/auth/device/code", json={})
    client.post("/v0/auth/token", json={"device_code": code_resp.json()["device_code"]})
    resp = client.post(
        "/v0/auth/approve",
        json={"user_code": code_resp.json()["user_code"], "roles": ["superuser"]},
    )
    assert resp.status_code == 400
    assert "unknown role" in resp.json()["error"]["message"].lower()


class TestDeviceCodeEndpoint:
    def test_issue_device_code(self, client: TestClient) -> None:
        resp = client.post("/v0/auth/device/code", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert "device_code" in data
        assert "user_code" in data
        assert "verification_uri" in data
        assert "expires_in" in data
        assert "interval" in data

    def test_user_code_format(self, client: TestClient) -> None:
        resp = client.post("/v0/auth/device/code", json={})
        user_code = resp.json()["user_code"]
        # Format: XXXX-YYYY
        parts = user_code.split("-")
        assert len(parts) == 2
        assert len(parts[0]) == 4
        assert len(parts[1]) == 4


class TestPollForToken:
    def test_authorization_pending(self, client: TestClient) -> None:
        # Issue a device code
        code_resp = client.post("/v0/auth/device/code", json={})
        device_code = code_resp.json()["device_code"]

        # Poll immediately — should be pending
        poll_resp = client.post("/v0/auth/token", json={"device_code": device_code})
        assert poll_resp.status_code == 200
        assert poll_resp.json()["error"] == "authorization_pending"

    def test_unknown_device_code_is_bad_request(self, client: TestClient) -> None:
        poll_resp = client.post(
            "/v0/auth/token", json={"device_code": "nonexistent-device-code"}
        )
        assert poll_resp.status_code == 400

    def test_missing_device_code_is_bad_request(self, client: TestClient) -> None:
        poll_resp = client.post("/v0/auth/token", json={})
        assert poll_resp.status_code == 400

    def test_approved_after_approve(self, client: TestClient) -> None:
        # Issue a device code
        code_resp = client.post("/v0/auth/device/code", json={})
        data = code_resp.json()
        device_code = data["device_code"]
        user_code = data["user_code"]

        # Admin approves
        approve_resp = client.post(
            "/v0/auth/approve",
            json={
                "user_code": user_code,
                "subject": "test@example.com",
                "roles": ["reader"],
            },
        )
        assert approve_resp.status_code == 200
        assert approve_resp.json()["ok"] is True

        # Poll → should return a token now
        poll_resp = client.post("/v0/auth/token", json={"device_code": device_code})
        assert poll_resp.status_code == 200
        token_data = poll_resp.json()
        assert "access_token" in token_data
        assert "refresh_token" in token_data


class TestApproveEndpoint:
    def test_approve_missing_user_code(self, client: TestClient) -> None:
        resp = client.post(
            "/v0/auth/approve",
            json={"subject": "x@example.com", "roles": ["reader"]},
        )
        assert resp.status_code == 400

    def test_approve_unknown_user_code(self, client: TestClient) -> None:
        resp = client.post(
            "/v0/auth/approve",
            json={"user_code": "XXXX-XXXX", "subject": "x@example.com"},
        )
        assert resp.status_code == 400
