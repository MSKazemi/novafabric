"""``nova db upgrade`` without the ``server`` extra fails clearly and touches nothing.

Both migration tracks run alembic + SQLAlchemy, which ship only in
``novafabric[server]``. Without them the metadata track used to fail inside the
child interpreter as a bare ``alembic exited with code 1`` and the registry track
as ``alembic upgrade failed (ModuleNotFoundError)``. The command now checks first
and names the extra. Local mode never needs it: the SQLite stores create their
own tables on first use.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.metadata_store import cli as md_cli

runner = CliRunner()

_REAL_FIND_SPEC = importlib.util.find_spec


def _without(*names: str) -> Any:
    def fake(name: str, package: str | None = None) -> Any:
        if name.split(".")[0] in names:
            return None
        return _REAL_FIND_SPEC(name, package)

    return fake


@pytest.fixture
def no_server_extra(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """alembic + SQLAlchemy look uninstalled; any alembic child process fails the test."""
    home = tmp_path / "home"
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.delenv("NOVAFABRIC_DB_PATH", raising=False)
    monkeypatch.delenv("NOVAFABRIC_METADATA_DB_PATH", raising=False)
    monkeypatch.setattr(importlib.util, "find_spec", _without("alembic", "sqlalchemy"))

    def _no_child(*_a: object, **_kw: object) -> None:
        raise AssertionError("alembic must not be launched without the server extra")

    monkeypatch.setattr(subprocess, "run", _no_child)
    return home


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize("track", ["metadata", "registry"])
def test_upgrade_names_the_server_extra_and_exits_2(no_server_extra: Path, track: str) -> None:
    result = runner.invoke(app, ["db", "upgrade", "--track", track, "--backend", "sqlite"])

    assert result.exit_code == 2, result.output
    assert isinstance(result.exception, SystemExit)  # no traceback
    out = _flat(result.output)
    assert "pip install 'novafabric[server]'" in out
    assert "alembic" in out and "sqlalchemy" in out
    assert "Local mode does not need this command" in out
    assert not no_server_extra.exists()  # no home, no metadata.db, no registry.db


def test_only_the_missing_module_is_named(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(importlib.util, "find_spec", _without("alembic"))
    result = runner.invoke(app, ["db", "upgrade"])
    assert result.exit_code == 2, result.output
    out = _flat(result.output)
    assert "needs alembic," in out
    assert "sqlalchemy" not in out


def test_programmatic_upgrade_reports_the_same(no_server_extra: Path) -> None:
    ok, message = md_cli.run_alembic_upgrade("sqlite", revision="head")
    assert ok is False
    assert "novafabric[server]" in message
    assert "alembic and sqlalchemy" in message
    assert not no_server_extra.exists()


def test_invalid_backend_is_still_reported_first(no_server_extra: Path) -> None:
    # Argument errors keep their own message and exit code.
    result = runner.invoke(app, ["db", "upgrade", "--backend", "mysql"])
    assert result.exit_code == 2
    assert "unknown backend" in result.output


def test_dependencies_present_reports_nothing_missing() -> None:
    pytest.importorskip("alembic")
    pytest.importorskip("sqlalchemy")
    assert md_cli._missing_migration_modules() == []


def test_find_spec_errors_count_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(name: str, package: str | None = None) -> Any:
        raise ValueError(f"{name}.__spec__ is None")

    monkeypatch.setattr(importlib.util, "find_spec", broken)
    assert md_cli._missing_migration_modules() == ["alembic", "sqlalchemy"]


def test_help_states_the_requirement() -> None:
    from typer.main import get_command

    db = get_command(app).commands["db"]  # type: ignore[attr-defined]
    upgrade = db.commands["upgrade"]
    assert "server extra" in (upgrade.help or "")
