"""CLI tests for the new HTTP-mode flags on `nova mcp-proxy` (C-3.4)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.cli.mcp_proxy import _parse_listen

runner = CliRunner()


def _capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "01TESTCLI0HTTPPROXY000000000"
    cap.mkdir(parents=True, exist_ok=True)
    # Touch the four jsonl files the writer expects.
    for name in ("model-calls.jsonl", "tool-calls.jsonl", "trace.jsonl", "assets.jsonl"):
        (cap / name).touch()
    return cap


# ── --listen parsing ─────────────────────────────────────────────────────────


class TestParseListen:
    def test_host_port_parsed(self) -> None:
        host, port = _parse_listen("0.0.0.0:8765")
        assert host == "0.0.0.0"
        assert port == 8765

    def test_just_port_uses_127001_default(self) -> None:
        host, port = _parse_listen(":8765")
        assert host == "127.0.0.1"
        assert port == 8765

    def test_missing_colon_errors(self) -> None:
        import typer

        with pytest.raises(typer.BadParameter, match="host:port"):
            _parse_listen("8765")

    def test_non_integer_port_errors(self) -> None:
        import typer

        with pytest.raises(typer.BadParameter, match="integer"):
            _parse_listen("127.0.0.1:not-a-port")

    def test_out_of_range_port_errors(self) -> None:
        import typer

        with pytest.raises(typer.BadParameter, match="1..65535"):
            _parse_listen("127.0.0.1:99999")


# ── --listen requires --upstream-url ─────────────────────────────────────────


class TestHttpModeFlagValidation:
    def test_listen_without_upstream_url_errors(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        result = runner.invoke(app, [
            "mcp-proxy",
            "--capsule-dir", str(cap),
            "--listen", "127.0.0.1:8765",
        ])
        assert result.exit_code != 0
        assert "upstream-url" in result.output.lower()

    def test_listen_with_command_errors(self, tmp_path: Path) -> None:
        """HTTP and stdio are mutually exclusive."""
        cap = _capsule(tmp_path)
        result = runner.invoke(app, [
            "mcp-proxy",
            "--capsule-dir", str(cap),
            "--listen", "127.0.0.1:8765",
            "--upstream-url", "http://upstream/mcp",
            "--",
            sys.executable, "-c", "pass",
        ])
        assert result.exit_code != 0
        assert "stdio" in result.output.lower() or "mutually exclusive" in result.output.lower()


class TestStdioModeBackwardCompat:
    def test_stdio_mode_still_requires_command(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        # Neither --listen nor a command — should fail.
        result = runner.invoke(app, [
            "mcp-proxy",
            "--capsule-dir", str(cap),
        ])
        assert result.exit_code != 0
        assert "COMMAND is required" in result.output


# ── HTTP-mode dispatch (verifies the CLI actually constructs an HTTP proxy) ──


class TestHttpModeDispatch:
    def test_listen_dispatches_to_http_proxy_run(self, tmp_path: Path) -> None:
        """Verify the CLI flow: --listen + --upstream-url constructs a
        MCPHttpProxy and calls run(). Mock run() so no real socket binds."""
        cap = _capsule(tmp_path)
        ran: dict[str, bool] = {"called": False}

        def fake_run(self) -> int:  # type: ignore[no-untyped-def]
            ran["called"] = True
            return 0

        with patch(
            "novafabric.proxy.mcp_proxy.MCPHttpProxy.run",
            new=fake_run,
        ):
            result = runner.invoke(app, [
                "mcp-proxy",
                "--capsule-dir", str(cap),
                "--listen", "127.0.0.1:18765",  # port 0 = OS-assigned
                "--upstream-url", "http://upstream/mcp",
            ])

        assert result.exit_code == 0, result.output
        assert ran["called"] is True

    def test_invalid_listen_format_errors_before_dispatch(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        result = runner.invoke(app, [
            "mcp-proxy",
            "--capsule-dir", str(cap),
            "--listen", "garbage",
            "--upstream-url", "http://upstream/mcp",
        ])
        assert result.exit_code != 0
        assert "host:port" in result.output


# ── CLI-side env fallback for --capsule-dir ──────────────────────────────────


class TestEnvFallbacks:
    def test_capsule_dir_falls_back_to_env(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        # Mock run() to avoid binding a socket, but the test path proves
        # --capsule-dir env fallback works.
        with patch("novafabric.proxy.mcp_proxy.MCPHttpProxy.run", return_value=0):
            saved = os.environ.get("NOVAFABRIC_CAPSULE_DIR")
            os.environ["NOVAFABRIC_CAPSULE_DIR"] = str(cap)
            try:
                result = runner.invoke(app, [
                    "mcp-proxy",
                    "--listen", "127.0.0.1:18765",
                    "--upstream-url", "http://upstream/mcp",
                ])
            finally:
                if saved is None:
                    os.environ.pop("NOVAFABRIC_CAPSULE_DIR", None)
                else:
                    os.environ["NOVAFABRIC_CAPSULE_DIR"] = saved
        assert result.exit_code == 0, result.output

    def test_missing_capsule_dir_auto_allocates(self, tmp_path: Path) -> None:
        """v0.6.5: no --capsule-dir, no env → auto-allocate under
        $PWD/.novafabric/runs/. Mocks run() so no real socket binds."""
        saved = os.environ.pop("NOVAFABRIC_CAPSULE_DIR", None)
        try:
            with patch("novafabric.proxy.mcp_proxy.MCPHttpProxy.run", return_value=0):
                cwd = os.getcwd()
                os.chdir(tmp_path)
                try:
                    result = runner.invoke(app, [
                        "mcp-proxy",
                        "--listen", "127.0.0.1:18765",
                        "--upstream-url", "http://upstream/mcp",
                    ])
                finally:
                    os.chdir(cwd)
        finally:
            if saved is not None:
                os.environ["NOVAFABRIC_CAPSULE_DIR"] = saved
        assert result.exit_code == 0, result.output
        runs = tmp_path / ".novafabric" / "runs"
        assert runs.exists()
