"""Round-4 default: ``nova classify from-capsule`` reads the store capture writes.

``--data-dir`` defaulted to ``~/novafabric-data`` (the Docker host data dir, never a CLI
default anywhere else), so ``nova classify from-capsule <run-id>`` after a default
``nova capture`` exited 2 "Capsule directory not found". The default is now capture's
store (``$NOVAFABRIC_CAPSULE_DIR``, else ``$NOVAFABRIC_HOME/capsules``), the same fix
M3 made for ``nova audit``. The help example passes a capsule *path*; that works too.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _capsule(root: Path, run_id: str = "cap-1") -> Path:
    cap = root / run_id
    cap.mkdir(parents=True)
    (cap / "capsule.yaml").write_text(
        f"run_id: {run_id}\nasset:\n  name: helper\n  description: a chat helper\n"
    )
    return cap


def _classify(*args: str) -> dict:
    result = runner.invoke(app, ["classify", "from-capsule", *args, "--json"])
    assert result.exit_code == 0, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    return json.loads(result.output)


def test_default_reads_nova_home_capsules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
    _capsule(tmp_path / "home" / "capsules")
    assert _classify("cap-1")["classification_capsule_id"] == "cap-1"


def test_default_reads_capsule_dir_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(tmp_path / "data" / "capsules"))
    _capsule(tmp_path / "data" / "capsules")
    assert _classify("cap-1")["classification_capsule_id"] == "cap-1"


def test_explicit_data_dir_keeps_its_meaning(tmp_path: Path) -> None:
    _capsule(tmp_path / "dd" / "capsules")
    assert _classify("cap-1", "--data-dir", str(tmp_path / "dd"))["classification_capsule_id"]


def test_a_capsule_path_works_as_the_help_example_shows(tmp_path: Path) -> None:
    cap = _capsule(tmp_path / "anywhere")
    assert _classify(str(cap))["classification_capsule_id"] == "cap-1"


def test_missing_names_the_directory_searched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
    result = runner.invoke(app, ["classify", "from-capsule", "nope"], env={"COLUMNS": "300"})
    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)
    assert str(tmp_path / "home" / "capsules" / "nope") in result.output.replace("\n", "")
