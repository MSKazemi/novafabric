"""CLI tests for `nova api-proxy` (ADR-0026 / v0.6.4)."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from novafabric.cli.api_proxy import _parse_listen
from novafabric.cli.main import app

runner = CliRunner()


def _capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "01TESTAPIPROXY00000000000000"
    cap.mkdir(parents=True, exist_ok=True)
    for name in ("model-calls.jsonl", "tool-calls.jsonl", "trace.jsonl", "assets.jsonl"):
        (cap / name).touch()
    return cap


class TestListenParser:
    def test_default_host_when_only_port(self) -> None:
        host, port = _parse_listen(":9000")
        assert host == "127.0.0.1"
        assert port == 9000

    def test_explicit_host_port(self) -> None:
        host, port = _parse_listen("0.0.0.0:8765")
        assert host == "0.0.0.0"
        assert port == 8765

    def test_missing_colon_errors(self) -> None:
        import typer
        with pytest.raises(typer.BadParameter, match="host:port"):
            _parse_listen("8765")


class TestRequiredFlags:
    def test_missing_upstream_url_errors(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        result = runner.invoke(app, [
            "api-proxy", "--capsule-dir", str(cap),
            "--listen", "127.0.0.1:18765",
        ])
        assert result.exit_code != 0
        assert "upstream-url" in result.output.lower()

    def test_missing_capsule_dir_auto_allocates(self, tmp_path: Path) -> None:
        """v0.6.5 ergonomic improvement: nova api-proxy without
        --capsule-dir auto-allocates a fresh ULID dir under
        $PWD/.novafabric/runs/. The proxy starts (mocked run()), and
        the auto-allocated dir is announced and created."""
        from unittest.mock import patch

        saved = os.environ.pop("NOVAFABRIC_CAPSULE_DIR", None)
        try:
            with patch("novafabric.proxy.api_proxy.ApiProxy.run", return_value=0):
                # Run from tmp_path so the auto-allocated dir lands there.
                cwd = os.getcwd()
                os.chdir(tmp_path)
                try:
                    result = runner.invoke(app, [
                        "api-proxy",
                        "--listen", "127.0.0.1:18765",
                        "--upstream-url", "https://api.openai.com",
                    ])
                finally:
                    os.chdir(cwd)
        finally:
            if saved is not None:
                os.environ["NOVAFABRIC_CAPSULE_DIR"] = saved
        assert result.exit_code == 0, result.output
        runs = tmp_path / ".novafabric" / "runs"
        assert runs.exists() and runs.is_dir()
        # Exactly one ULID-named subdirectory should have been created.
        children = [c for c in runs.iterdir() if c.is_dir()]
        assert len(children) == 1
        # Capsule dir was announced in the output.
        assert "Allocated capsule" in result.output


class TestDispatch:
    def test_valid_invocation_constructs_proxy_and_runs(
        self, tmp_path: Path
    ) -> None:
        cap = _capsule(tmp_path)
        ran: dict[str, bool] = {"called": False}

        def fake_run(self) -> int:  # type: ignore[no-untyped-def]
            ran["called"] = True
            return 0

        with patch("novafabric.proxy.api_proxy.ApiProxy.run", new=fake_run):
            result = runner.invoke(app, [
                "api-proxy",
                "--capsule-dir", str(cap),
                "--listen", "127.0.0.1:18765",
                "--upstream-url", "https://api.openai.com",
            ])
        assert result.exit_code == 0, result.output
        assert ran["called"] is True

    def test_capsule_dir_env_fallback(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        with patch("novafabric.proxy.api_proxy.ApiProxy.run", return_value=0):
            saved = os.environ.get("NOVAFABRIC_CAPSULE_DIR")
            os.environ["NOVAFABRIC_CAPSULE_DIR"] = str(cap)
            try:
                result = runner.invoke(app, [
                    "api-proxy",
                    "--listen", "127.0.0.1:18765",
                    "--upstream-url", "https://api.openai.com",
                ])
            finally:
                if saved is None:
                    os.environ.pop("NOVAFABRIC_CAPSULE_DIR", None)
                else:
                    os.environ["NOVAFABRIC_CAPSULE_DIR"] = saved
        assert result.exit_code == 0, result.output
