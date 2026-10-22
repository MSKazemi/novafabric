"""Round-4 default: ``nova retention`` sweeps the capsule store capture writes to.

``cli/retention.py`` defaulted ``--capsule-dir`` to ``$NOVAFABRIC_HOME/capsules`` and
ignored ``NOVAFABRIC_CAPSULE_DIR``, while ``nova capture`` honours it — so with the env
var set (the Docker layout) a retention sweep scanned an empty directory and reported
nothing due, while every capsule sat in the store it never looked at.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import novafabric.cli.retention as retention_cli
from novafabric.cli.main import app

runner = CliRunner()


def test_default_follows_capsule_dir_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
    assert retention_cli._default_capsule_dir() == tmp_path / "home" / "capsules"
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(tmp_path / "data" / "capsules"))
    assert retention_cli._default_capsule_dir() == tmp_path / "data" / "capsules"


@pytest.mark.parametrize("command", ["plan", "apply"])
def test_commands_sweep_the_capture_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    store = tmp_path / "data" / "capsules"
    store.mkdir(parents=True)
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(store))
    seen: dict[str, Any] = {}

    class _Stop(Exception):
        pass

    def spy(registry: str, capsule_dir: Path, *args: Any, **kwargs: Any) -> Any:
        seen["capsule_dir"] = capsule_dir
        raise _Stop

    monkeypatch.setattr(retention_cli, "_build_plan", spy)
    args = ["retention", command, "--registry", "r1"]
    if command == "apply":
        args.append("--yes")
    result = runner.invoke(app, args)
    assert isinstance(result.exception, _Stop), (result.exception, result.output)
    assert seen.get("capsule_dir") == store, result.output


def test_help_names_the_env_var() -> None:
    result = runner.invoke(app, ["retention", "plan", "--help"], env={"COLUMNS": "300"})
    assert "NOVAFABRIC_CAPSULE_DIR" in result.output
