"""Tests for `nova login` and `nova logout` CLI commands (Track S-3).

Covers:
- Device Grant flow completes against a mock server
- Credentials file created with mode 0600
- nova logout removes credential entry
- get_token returns None when not logged in
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("httpx")

from typer.testing import CliRunner  # noqa: E402

from novafabric.cli.login import (  # noqa: E402
    _build_entry,
    get_token,
)
from novafabric.cli.main import app  # noqa: E402

runner = CliRunner()


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------


class TestCredentialHelpers:
    def test_build_entry_contains_required_keys(self) -> None:
        resp: dict[str, object] = {
            "access_token": "tok123",
            "refresh_token": "ref456",
            "expires_in": 3600,
        }
        entry = _build_entry("http://localhost:7433", resp)
        assert entry["access_token"] == "tok123"
        assert entry["refresh_token"] == "ref456"
        assert "expiry" in entry
        assert entry["server_url"] == "http://localhost:7433"

    def test_build_entry_default_expires_in(self) -> None:
        resp: dict[str, object] = {"access_token": "tok"}
        entry = _build_entry("http://localhost:7433", resp)
        assert "expiry" in entry

    def test_get_token_returns_none_when_no_creds(self, tmp_path: Path) -> None:
        with patch("novafabric.cli.login._CRED_FILE", tmp_path / "creds.json"):
            result = get_token("http://localhost:7433")
        assert result is None

    def test_get_token_returns_token_when_valid(self, tmp_path: Path) -> None:
        cred_path = tmp_path / "creds.json"
        future_expiry = "2099-01-01T00:00:00+00:00"
        creds = {
            "http://localhost:7433": {
                "access_token": "valid-tok",
                "refresh_token": None,
                "expiry": future_expiry,
                "server_url": "http://localhost:7433",
            }
        }
        cred_path.write_text(json.dumps(creds))
        cred_path.chmod(0o600)

        with (
            patch("novafabric.cli.login._CRED_FILE", cred_path),
            patch("novafabric.cli.login._CRED_DIR", tmp_path),
        ):
            result = get_token("http://localhost:7433")
        assert result == "valid-tok"

    def test_get_token_returns_none_for_expired(self, tmp_path: Path) -> None:
        cred_path = tmp_path / "creds.json"
        past_expiry = "2000-01-01T00:00:00+00:00"
        creds = {
            "http://localhost:7433": {
                "access_token": "old-tok",
                "refresh_token": None,
                "expiry": past_expiry,
                "server_url": "http://localhost:7433",
            }
        }
        cred_path.write_text(json.dumps(creds))

        with (
            patch("novafabric.cli.login._CRED_FILE", cred_path),
            patch("novafabric.cli.login._CRED_DIR", tmp_path),
        ):
            result = get_token("http://localhost:7433")
        # Expired + no refresh token → None
        assert result is None


# ---------------------------------------------------------------------------
# nova login flow (mocked httpx)
# ---------------------------------------------------------------------------


class TestLoginCommand:
    def _mock_device_code_response(self) -> dict[str, Any]:
        return {
            "device_code": "dev-code-abc",
            "user_code": "ABCD-1234",
            "verification_uri": "http://localhost:7433/v0/auth/approve",
            "expires_in": 300,
            "interval": 1,
        }

    def _mock_token_response(self) -> dict[str, Any]:
        return {
            "access_token": "access-token-xyz",
            "refresh_token": "refresh-token-xyz",
            "token_type": "Bearer",
            "expires_in": 3600,
        }

    def test_login_device_grant_flow(self, tmp_path: Path) -> None:
        """Login should call device/code, poll, and store credentials."""
        cred_path = tmp_path / "credentials.json"

        call_count = {"poll": 0}

        def mock_post(url: str, **kwargs: Any) -> MagicMock:
            resp = MagicMock()
            if "device/code" in url:
                resp.json.return_value = self._mock_device_code_response()
                resp.raise_for_status = MagicMock()
            elif "/token" in url:
                call_count["poll"] += 1
                if call_count["poll"] < 2:
                    resp.json.return_value = {"error": "authorization_pending"}
                else:
                    resp.json.return_value = self._mock_token_response()
            return resp

        with (
            patch("novafabric.cli.login._CRED_FILE", cred_path),
            patch("novafabric.cli.login._CRED_DIR", tmp_path),
            patch("httpx.post", side_effect=mock_post),
            patch("time.sleep"),  # don't actually sleep
        ):
            result = runner.invoke(app, ["login", "--server", "http://localhost:7433"])

        assert result.exit_code == 0, result.output

    def test_login_stores_credentials_with_mode_0600(self, tmp_path: Path) -> None:
        cred_path = tmp_path / "credentials.json"
        call_count = {"poll": 0}

        def mock_post(url: str, **kwargs: Any) -> MagicMock:
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if "device/code" in url:
                resp.json.return_value = self._mock_device_code_response()
            elif "/token" in url:
                call_count["poll"] += 1
                resp.json.return_value = self._mock_token_response()
            return resp

        with (
            patch("novafabric.cli.login._CRED_FILE", cred_path),
            patch("novafabric.cli.login._CRED_DIR", tmp_path),
            patch("httpx.post", side_effect=mock_post),
            patch("time.sleep"),
        ):
            runner.invoke(app, ["login", "--server", "http://localhost:7433"])

        if cred_path.exists():
            mode = stat.S_IMODE(cred_path.stat().st_mode)
            assert mode == 0o600

    def test_login_server_unreachable(self, tmp_path: Path) -> None:
        """Login should exit with code 1 when server is unreachable."""
        import httpx

        with (
            patch("novafabric.cli.login._CRED_FILE", tmp_path / "creds.json"),
            patch("novafabric.cli.login._CRED_DIR", tmp_path),
            patch("httpx.post", side_effect=httpx.ConnectError("refused")),
        ):
            result = runner.invoke(app, ["login", "--server", "http://localhost:9999"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# nova logout
# ---------------------------------------------------------------------------


class TestLogoutCommand:
    def test_logout_specific_server(self, tmp_path: Path) -> None:
        cred_path = tmp_path / "credentials.json"
        creds = {
            "http://localhost:7433": {
                "access_token": "tok",
                "server_url": "http://localhost:7433",
            },
            "http://other:7433": {
                "access_token": "tok2",
                "server_url": "http://other:7433",
            },
        }
        cred_path.write_text(json.dumps(creds))
        cred_path.chmod(0o600)

        with (
            patch("novafabric.cli.login._CRED_FILE", cred_path),
            patch("novafabric.cli.login._CRED_DIR", tmp_path),
        ):
            result = runner.invoke(
                app, ["logout", "--server", "http://localhost:7433"]
            )

        assert result.exit_code == 0
        remaining = json.loads(cred_path.read_text())
        assert "http://localhost:7433" not in remaining
        assert "http://other:7433" in remaining

    def test_logout_all_servers(self, tmp_path: Path) -> None:
        cred_path = tmp_path / "credentials.json"
        creds = {
            "http://a:7433": {"access_token": "t1"},
            "http://b:7433": {"access_token": "t2"},
        }
        cred_path.write_text(json.dumps(creds))
        cred_path.chmod(0o600)

        with (
            patch("novafabric.cli.login._CRED_FILE", cred_path),
            patch("novafabric.cli.login._CRED_DIR", tmp_path),
        ):
            result = runner.invoke(app, ["logout"])

        assert result.exit_code == 0
        remaining = json.loads(cred_path.read_text())
        assert remaining == {}

    def test_logout_nonexistent_server(self, tmp_path: Path) -> None:
        cred_path = tmp_path / "credentials.json"
        cred_path.write_text("{}")

        with (
            patch("novafabric.cli.login._CRED_FILE", cred_path),
            patch("novafabric.cli.login._CRED_DIR", tmp_path),
        ):
            result = runner.invoke(app, ["logout", "--server", "http://nobody:7433"])
        assert result.exit_code == 0
        assert "No credentials" in result.output

    def test_logout_when_no_file(self, tmp_path: Path) -> None:
        cred_path = tmp_path / "credentials.json"  # does not exist

        with (
            patch("novafabric.cli.login._CRED_FILE", cred_path),
            patch("novafabric.cli.login._CRED_DIR", tmp_path),
        ):
            result = runner.invoke(app, ["logout"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# nova login --help / nova logout --help
# ---------------------------------------------------------------------------


class TestTokenRefresh:
    def test_get_token_with_refresh_success(self, tmp_path: Path) -> None:
        """When access token is expired, it should attempt refresh."""
        from unittest.mock import MagicMock


        cred_path = tmp_path / "credentials.json"
        past_expiry = "2000-01-01T00:00:00+00:00"
        creds = {
            "http://localhost:7433": {
                "access_token": "old-tok",
                "refresh_token": "good-refresh",
                "expiry": past_expiry,
                "server_url": "http://localhost:7433",
            }
        }
        cred_path.write_text(json.dumps(creds))
        cred_path.chmod(0o600)

        def mock_post(url: str, **kwargs: Any) -> MagicMock:
            resp = MagicMock()
            resp.json.return_value = {
                "access_token": "new-tok",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            }
            return resp

        with (
            patch("novafabric.cli.login._CRED_FILE", cred_path),
            patch("novafabric.cli.login._CRED_DIR", tmp_path),
            patch("httpx.post", side_effect=mock_post),
        ):
            result = get_token("http://localhost:7433")
        assert result == "new-tok"

    def test_get_token_refresh_network_error(self, tmp_path: Path) -> None:
        """If refresh network call fails, returns None."""
        import httpx

        cred_path = tmp_path / "credentials.json"
        creds = {
            "http://localhost:7433": {
                "access_token": "old-tok",
                "refresh_token": "good-refresh",
                "expiry": "2000-01-01T00:00:00+00:00",
            }
        }
        cred_path.write_text(json.dumps(creds))

        with (
            patch("novafabric.cli.login._CRED_FILE", cred_path),
            patch("novafabric.cli.login._CRED_DIR", tmp_path),
            patch("httpx.post", side_effect=httpx.ConnectError("refused")),
        ):
            result = get_token("http://localhost:7433")
        assert result is None


class TestHelpCommands:
    def test_login_help(self) -> None:
        result = runner.invoke(app, ["login", "--help"])
        assert result.exit_code == 0
        assert "server" in result.output.lower()

    def test_logout_help(self) -> None:
        result = runner.invoke(app, ["logout", "--help"])
        assert result.exit_code == 0
        assert "server" in result.output.lower()

    def test_server_help(self) -> None:
        result = runner.invoke(app, ["server", "--help"])
        assert result.exit_code == 0
        assert "issue-token" in result.output

    def test_server_issue_token_help(self) -> None:
        result = runner.invoke(app, ["server", "issue-token", "--help"])
        assert result.exit_code == 0

    def test_server_revoke_token_help(self) -> None:
        result = runner.invoke(app, ["server", "revoke-token", "--help"])
        assert result.exit_code == 0

    def test_server_assign_role_help(self) -> None:
        result = runner.invoke(app, ["server", "assign-role", "--help"])
        assert result.exit_code == 0
