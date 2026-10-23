"""M5 — ``nova login`` is honest that it speaks only to the DEMO device grant.

``nova login`` posts to ``/v0/auth/device/code``, which the server mounts only
with ``NOVAFABRIC_SERVER_DEMO_DEVICE_GRANT`` (off by default), and whose HS256
tokens the real verifier never honours (it pins asymmetric algorithms / the
opaque local token). ADR-0018 names RFC 8628 against the IdP as the target;
that is not implemented. So against a production server the command failed
with a bare ``404 Not Found``, and against a demo server it "succeeded" with a
credential no API route accepts. These tests drive the CLI against the REAL
server app (routed through a TestClient), not a hand-written mock, so the
claims the CLI makes are checked against what the server actually does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from novafabric.cli.main import app  # noqa: E402
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import ServerConfig  # noqa: E402

SERVER = "http://nova.test:7433"
runner = CliRunner()


def _client(tmp_path: Path, *, demo: bool) -> TestClient:
    cfg = ServerConfig(db_path=str(tmp_path / "registry.db"), demo_device_grant=demo)
    return TestClient(create_app(cfg), raise_server_exceptions=False)


def _route_httpx_to(client: TestClient, *, approve: bool) -> Any:
    """Send ``httpx.post`` to *client*; approve the device code on the first poll."""
    state: dict[str, Any] = {}

    def _post(url: str, **kwargs: Any) -> Any:
        path = url.removeprefix(SERVER)
        kwargs.pop("timeout", None)
        resp = client.post(path, **kwargs)
        if path == "/v0/auth/device/code" and resp.status_code == 200:
            state["user_code"] = resp.json()["user_code"]
        if path == "/v0/auth/token" and approve and "approved" not in state:
            client.post(
                "/v0/auth/approve",
                json={"user_code": state["user_code"], "subject": "alice", "roles": ["reader"]},
            )
            state["approved"] = True
            resp = client.post(path, **kwargs)
        return resp

    return _post


def _login(tmp_path: Path, post: Any) -> Any:
    with (
        patch("novafabric.cli.login._CRED_FILE", tmp_path / "credentials.json"),
        patch("novafabric.cli.login._CRED_DIR", tmp_path),
        patch("httpx.post", side_effect=post),
        patch("time.sleep"),
    ):
        return runner.invoke(app, ["login", "--server", SERVER])


def test_help_says_demo_only_and_names_the_real_alternatives() -> None:
    result = runner.invoke(app, ["login", "--help"])
    assert result.exit_code == 0
    text = " ".join(result.output.split())
    assert "demo" in text.lower()
    assert "NOVAFABRIC_SERVER_DEMO_DEVICE_GRANT" in text
    assert "does not contact your identity provider" in text
    assert "issue-token" in text


def test_production_server_gets_an_explanation_not_a_bare_404(tmp_path: Path) -> None:
    client = _client(tmp_path, demo=False)
    result = _login(tmp_path, _route_httpx_to(client, approve=False))
    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 1
    assert "does not expose the demo device grant" in result.stderr
    assert "NOVAFABRIC_SERVER_DEMO_DEVICE_GRANT" in result.stderr
    assert "Bearer" in result.stderr and "issue-token" in result.stderr
    assert not (tmp_path / "credentials.json").exists()


def test_the_demo_token_is_really_refused_by_the_servers_api(tmp_path: Path) -> None:
    """The fact the success-path warning relies on — checked, not asserted in prose."""
    client = _client(tmp_path, demo=True)
    code = client.post("/v0/auth/device/code", json={}).json()
    client.post(
        "/v0/auth/approve",
        json={"user_code": code["user_code"], "subject": "alice", "roles": ["admin"]},
    )
    token = client.post("/v0/auth/token", json={"device_code": code["device_code"]}).json()
    assert "access_token" in token
    resp = client.get("/v0/assets", headers={"Authorization": f"Bearer {token['access_token']}"})
    assert resp.status_code == 401


def test_demo_login_succeeds_but_warns_the_token_is_not_accepted(tmp_path: Path) -> None:
    client = _client(tmp_path, demo=True)
    result = _login(tmp_path, _route_httpx_to(client, approve=True))
    assert result.exit_code == 0, result.output
    assert (tmp_path / "credentials.json").exists()
    assert "demo" in result.stderr.lower()
    assert "not accepted" in result.stderr
