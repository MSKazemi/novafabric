"""Tests for secure-by-default local server auth (ADR-0184, first slice).

Covers:
- OIDC disabled + no token header → 401
- correct local token → authenticated as local-admin with role admin
- wrong token → 401
- insecure_no_auth opt-out → anonymous admin works + startup warning logged
- non-loopback bind + insecure_no_auth without confirmation → refused
- token file created mode 0600; env token wins over file
- CLI flags: --insecure-no-auth, --i-know-this-is-public, token startup logging
"""

from __future__ import annotations

import logging
import stat
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from click.testing import Result  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from novafabric._paths import server_token_path  # noqa: E402
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import (  # noqa: E402
    InsecureBindError,
    ServerConfig,
    check_insecure_bind,
)
from novafabric.server.local_token import (  # noqa: E402
    TOKEN_ENV,
    ensure_local_token,
    generate_token,
    write_token_file,
)

TEST_TOKEN = "test-local-token-abc123"


def _client(tmp_path: Path, **cfg_kwargs: object) -> TestClient:
    cfg = ServerConfig(db_path=str(tmp_path / "test.db"), **cfg_kwargs)  # type: ignore[arg-type]
    return TestClient(create_app(cfg), raise_server_exceptions=False)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Default local mode: token required
# ---------------------------------------------------------------------------


class TestLocalTokenRequired:
    def test_no_token_returns_401(self, tmp_path: Path) -> None:
        client = _client(tmp_path, local_token=TEST_TOKEN)
        resp = client.get("/v0/assets")
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["code"] == "unauthenticated"
        assert "local token" in body["error"]["message"].lower()

    def test_correct_token_returns_200(self, tmp_path: Path) -> None:
        client = _client(tmp_path, local_token=TEST_TOKEN)
        resp = client.get("/v0/assets", headers=_bearer(TEST_TOKEN))
        assert resp.status_code == 200

    def test_correct_token_is_admin(self, tmp_path: Path) -> None:
        """The local token maps to role admin — admin-gated endpoint succeeds."""
        client = _client(tmp_path, local_token=TEST_TOKEN)
        resp = client.post("/v0/admin/flush-jwks", headers=_bearer(TEST_TOKEN))
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_wrong_token_returns_401(self, tmp_path: Path) -> None:
        client = _client(tmp_path, local_token=TEST_TOKEN)
        resp = client.get("/v0/assets", headers=_bearer("wrong-token"))
        assert resp.status_code == 401
        assert "invalid local token" in resp.json()["error"]["message"].lower()

    def test_malformed_header_returns_401(self, tmp_path: Path) -> None:
        client = _client(tmp_path, local_token=TEST_TOKEN)
        resp = client.get("/v0/assets", headers={"Authorization": TEST_TOKEN})
        assert resp.status_code == 401

    def test_health_stays_unauthenticated(self, tmp_path: Path) -> None:
        client = _client(tmp_path, local_token=TEST_TOKEN)
        assert client.get("/health").status_code == 200

    def test_lazy_resolution_persists_token_file(self, tmp_path: Path) -> None:
        """No config token, no env → verify_token resolves and persists a token."""
        client = _client(tmp_path)  # local_token=None
        assert client.get("/v0/assets").status_code == 401  # triggers resolution
        path = server_token_path()
        assert path.exists()
        token = path.read_text().strip()
        resp = client.get("/v0/assets", headers=_bearer(token))
        assert resp.status_code == 200

    def test_env_token_authenticates_via_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(TOKEN_ENV, "env-pinned-token")
        client = _client(tmp_path)  # env override lands in cfg.local_token
        assert client.get("/v0/assets", headers=_bearer("env-pinned-token")).status_code == 200
        assert client.get("/v0/assets", headers=_bearer("other")).status_code == 401


# ---------------------------------------------------------------------------
# Insecure opt-out
# ---------------------------------------------------------------------------


class TestInsecureOptOut:
    def test_insecure_flag_restores_anonymous_admin(self, tmp_path: Path) -> None:
        client = _client(tmp_path, insecure_no_auth=True)
        assert client.get("/v0/assets").status_code == 200
        assert client.post("/v0/admin/flush-jwks").status_code == 200

    def test_insecure_flag_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="novafabric.server.app"):
            _client(tmp_path, insecure_no_auth=True)
        assert any(
            "INSECURE" in rec.message and rec.levelno == logging.WARNING
            for rec in caplog.records
        )

    def test_env_opt_out(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVAFABRIC_SERVER_INSECURE_NO_AUTH", "true")
        cfg = ServerConfig(db_path=str(tmp_path / "test.db"))
        assert cfg.insecure_no_auth is True
        client = TestClient(create_app(cfg), raise_server_exceptions=False)
        assert client.get("/v0/assets").status_code == 200


# ---------------------------------------------------------------------------
# Non-loopback + insecure refusal (all four combinations)
# ---------------------------------------------------------------------------


class TestInsecureBindRefusal:
    def test_public_insecure_unconfirmed_refused(self) -> None:
        with pytest.raises(ValidationError, match="non-loopback"):
            ServerConfig(host="0.0.0.0", insecure_no_auth=True)

    def test_public_insecure_confirmed_allowed(self) -> None:
        cfg = ServerConfig(host="0.0.0.0", insecure_no_auth=True, i_know_this_is_public=True)
        assert cfg.insecure_no_auth is True

    def test_loopback_insecure_unconfirmed_allowed(self) -> None:
        cfg = ServerConfig(host="127.0.0.1", insecure_no_auth=True)
        assert cfg.insecure_no_auth is True

    def test_public_secure_allowed(self) -> None:
        cfg = ServerConfig(host="0.0.0.0")
        assert cfg.insecure_no_auth is False

    def test_localhost_name_is_loopback(self) -> None:
        cfg = ServerConfig(host="localhost", insecure_no_auth=True)
        assert cfg.insecure_no_auth is True

    def test_ipv6_loopback_allowed(self) -> None:
        cfg = ServerConfig(host="::1", insecure_no_auth=True)
        assert cfg.insecure_no_auth is True

    def test_check_insecure_bind_after_mutation(self) -> None:
        """CLI-style post-validation mutation is re-checked by the helper."""
        cfg = ServerConfig(insecure_no_auth=True)  # loopback: fine
        cfg.host = "0.0.0.0"  # assignment skips model validation
        with pytest.raises(InsecureBindError, match="non-loopback"):
            check_insecure_bind(cfg)

    def test_env_combination_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVAFABRIC_SERVER_HOST", "0.0.0.0")
        monkeypatch.setenv("NOVAFABRIC_SERVER_INSECURE_NO_AUTH", "1")
        with pytest.raises(ValidationError, match="non-loopback"):
            ServerConfig()


# ---------------------------------------------------------------------------
# Token resolution + file lifecycle
# ---------------------------------------------------------------------------


class TestTokenResolution:
    def test_token_file_created_0600(self) -> None:
        token, path = ensure_local_token()
        assert path == server_token_path()
        assert path.exists()
        assert path.read_text().strip() == token
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600

    def test_existing_file_token_reused(self) -> None:
        write_token_file("stored-token")
        assert generate_token() == "stored-token"
        token, _ = ensure_local_token()
        assert token == "stored-token"

    def test_env_token_wins_over_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        write_token_file("file-token")
        monkeypatch.setenv(TOKEN_ENV, "env-token")
        assert generate_token() == "env-token"
        token, _ = ensure_local_token()
        assert token == "env-token"

    def test_fresh_token_when_no_sources(self) -> None:
        assert not server_token_path().exists()
        token = generate_token()
        assert len(token) >= 32

    def test_two_fresh_tokens_differ(self) -> None:
        assert generate_token() != generate_token()


# ---------------------------------------------------------------------------
# CLI: nova server start flags
# ---------------------------------------------------------------------------


runner = CliRunner()


class _FakeUvicornRun:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, app_obj: object, host: str, port: int) -> None:
        self.calls.append({"host": host, "port": port})


class TestStartCli:
    @pytest.fixture
    def fake_run(self, monkeypatch: pytest.MonkeyPatch) -> _FakeUvicornRun:
        import uvicorn

        fake = _FakeUvicornRun()
        monkeypatch.setattr(uvicorn, "run", fake)
        return fake

    def _invoke(self, tmp_path: Path, *args: str) -> Result:
        from novafabric.cli.main import app

        missing_cfg = tmp_path / "no-such-server.yaml"
        return runner.invoke(app, ["server", "start", "--config", str(missing_cfg), *args])

    def test_public_insecure_unconfirmed_refused(
        self, tmp_path: Path, fake_run: _FakeUvicornRun
    ) -> None:
        result = self._invoke(tmp_path, "--host", "0.0.0.0", "--insecure-no-auth")
        assert result.exit_code == 2
        assert fake_run.calls == []

    def test_public_insecure_confirmed_starts(
        self, tmp_path: Path, fake_run: _FakeUvicornRun
    ) -> None:
        result = self._invoke(
            tmp_path, "--host", "0.0.0.0", "--insecure-no-auth", "--i-know-this-is-public"
        )
        assert result.exit_code == 0, result.output
        assert fake_run.calls == [{"host": "0.0.0.0", "port": 7433}]

    def test_start_prints_token_to_terminal_never_to_the_logger(
        self,
        tmp_path: Path,
        fake_run: _FakeUvicornRun,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The bearer token goes to the terminal only.

        Security fix (enterprise audit 2026-07-30): it used to be written to
        the application logger at INFO, which lands in aggregated logs /
        journald / log files. The terminal now carries the token and the
        usage hint (with the secret elided in the hint); the logger records
        only the non-secret token-file path and the pinning env var.
        """
        with caplog.at_level(logging.INFO, logger="novafabric.server"):
            result = self._invoke(tmp_path)
        assert result.exit_code == 0, result.output
        token = server_token_path().read_text().strip()

        # The secret must never reach a log record …
        assert not any(token in rec.getMessage() for rec in caplog.records), (
            "bearer token leaked into the application logger"
        )
        # … and neither should the copy-pasteable Bearer hint carrying it.
        assert not any("Authorization: Bearer" in rec.getMessage() for rec in caplog.records)
        # The logger still tells an operator where to find the token.
        assert any("Local auth token stored at" in rec.getMessage() for rec in caplog.records)
        # The operator gets the real token, and the hint, on the terminal.
        assert token in result.output
        assert "Authorization: Bearer" in result.output

    def test_start_insecure_logs_warning(
        self,
        tmp_path: Path,
        fake_run: _FakeUvicornRun,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING):
            result = self._invoke(tmp_path, "--insecure-no-auth")
        assert result.exit_code == 0, result.output
        assert any(
            "INSECURE" in rec.getMessage() and rec.levelno == logging.WARNING
            for rec in caplog.records
        )
