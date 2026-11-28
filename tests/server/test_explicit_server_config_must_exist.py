"""An explicit ``--config`` that cannot be used is an error, never silent defaults.

``nova server start --config <path>`` used to start on built-in defaults when the
path did not exist (a typo in a systemd unit gave a running server with the wrong
backend, host and auth settings), and a malformed file crashed with a traceback.
``load_config(path, strict=True)`` now refuses both; the default location stays
optional, so a plain ``nova server start`` is unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.server import config as config_mod
from novafabric.server.config import ServerConfig, ServerConfigFileError, load_config

runner = CliRunner()


# ---------------------------------------------------------------------------
# load_config(strict=True)
# ---------------------------------------------------------------------------


def test_strict_missing_path_raises(tmp_path: Path) -> None:
    missing = tmp_path / "no-such.yaml"
    with pytest.raises(ServerConfigFileError, match="does not exist"):
        load_config(missing, strict=True)


def test_non_strict_missing_path_keeps_the_old_fallback(tmp_path: Path) -> None:
    # Callers that did not opt in (the preflight, the dashboard) are unchanged.
    assert load_config(tmp_path / "no-such.yaml") == ServerConfig()


def test_strict_without_a_path_still_treats_the_default_as_optional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_mod, "_DEFAULT_CONFIG_PATH", tmp_path / "absent.yaml")
    assert load_config(None, strict=True) == ServerConfig()


def test_strict_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(ServerConfigFileError, match="not a regular file"):
        load_config(tmp_path, strict=True)


def test_strict_empty_file_is_defaults(tmp_path: Path) -> None:
    empty = tmp_path / "server.yaml"
    empty.write_text("")
    assert load_config(empty, strict=True) == ServerConfig()


def test_strict_valid_file_is_loaded(tmp_path: Path) -> None:
    path = tmp_path / "server.yaml"
    path.write_text("port: 9123\n")
    assert load_config(path, strict=True).port == 9123


def test_strict_malformed_yaml_names_the_line_but_not_the_content(tmp_path: Path) -> None:
    path = tmp_path / "server.yaml"
    path.write_text('host: 127.0.0.1\npostgres_dsn: "postgresql://u:hunter2@db/x\n')
    with pytest.raises(ServerConfigFileError, match="not valid YAML") as info:
        load_config(path, strict=True)
    message = str(info.value)
    assert "line" in message
    assert "hunter2" not in message


def test_strict_non_mapping_raises(tmp_path: Path) -> None:
    path = tmp_path / "server.yaml"
    path.write_text("- host\n- port\n")
    with pytest.raises(ServerConfigFileError, match="YAML mapping"):
        load_config(path, strict=True)


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root reads mode-000 files"
)
def test_strict_unreadable_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "server.yaml"
    path.write_text("port: 9123\n")
    path.chmod(0)
    try:
        with pytest.raises(ServerConfigFileError, match="not readable"):
            load_config(path, strict=True)
    finally:
        path.chmod(0o600)


# ---------------------------------------------------------------------------
# CLI surfaces
# ---------------------------------------------------------------------------


@pytest.fixture
def no_uvicorn_run(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record (rather than perform) any server launch."""
    uvicorn = pytest.importorskip("uvicorn")
    pytest.importorskip("fastapi")
    calls: list[object] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    return calls


def test_server_start_refuses_a_missing_config(
    tmp_path: Path, no_uvicorn_run: list[object]
) -> None:
    from novafabric.cli.main import app

    missing = tmp_path / "typo-server.yaml"
    result = runner.invoke(app, ["server", "start", "--config", str(missing)])
    assert result.exit_code == 2, result.output
    assert "does not exist" in result.output
    assert "--config" in result.output
    assert no_uvicorn_run == []  # nothing started on built-in defaults
    # Refused before the local token is minted (ADR-0184).
    assert not (Path(os.environ["NOVAFABRIC_HOME"]) / ".server-token").exists()


def test_server_start_refuses_malformed_yaml_without_a_traceback(
    tmp_path: Path, no_uvicorn_run: list[object]
) -> None:
    from novafabric.cli.main import app

    bad = tmp_path / "server.yaml"
    bad.write_text("host: [\n")
    result = runner.invoke(app, ["server", "start", "--config", str(bad)])
    assert result.exit_code == 2, result.output
    assert isinstance(result.exception, SystemExit)
    assert "not valid YAML" in result.output
    assert no_uvicorn_run == []


def test_server_start_with_an_existing_config_still_starts(
    tmp_path: Path, no_uvicorn_run: list[object]
) -> None:
    from novafabric.cli.main import app

    path = tmp_path / "server.yaml"
    path.write_text("port: 9124\n")
    result = runner.invoke(
        app, ["server", "start", "--config", str(path), "--insecure-no-auth"]
    )
    assert result.exit_code == 0, result.output
    assert len(no_uvicorn_run) == 1


def test_server_start_help_states_the_contract() -> None:
    from typer.main import get_command

    from novafabric.cli.main import app

    server = get_command(app).commands["server"]  # type: ignore[attr-defined]
    start = server.commands["start"]
    option = next(p for p in start.params if p.name == "config")
    assert "must be a readable YAML mapping" in (option.help or "")


def test_saml_metadata_refuses_a_missing_config(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from novafabric.cli.main import app

    result = runner.invoke(
        app, ["server", "saml-metadata", "--config", str(tmp_path / "no-such.yaml")]
    )
    assert result.exit_code == 2, result.output
    assert "does not exist" in result.output


def test_usage_reconcile_refuses_a_missing_config(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from novafabric.cli.main import app

    result = runner.invoke(
        app,
        ["server", "usage", "reconcile", "--config", str(tmp_path / "no-such.yaml")],
    )
    assert result.exit_code == 2, result.output
    assert "does not exist" in result.output
