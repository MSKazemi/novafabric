"""``nova import`` without ``--capsule-dir`` lands in the store ``nova capture`` writes to.

The import service already resolved ``NOVAFABRIC_CAPSULE_DIR`` first; its ``--help``
and the CLI reference said ``$NOVAFABRIC_HOME/capsules``. This pins the behaviour
the corrected help text now states: flag > ``NOVAFABRIC_CAPSULE_DIR`` >
``$NOVAFABRIC_HOME/capsules``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from import_blob.helpers import export_capsules, make_capsule
from novafabric.cli.main import app
from novafabric.evidence.signing import LocalSigner

runner = CliRunner()


@pytest.fixture()
def export_dir(source_root: Path, tmp_path: Path, signer: LocalSigner) -> Path:
    return export_capsules([make_capsule(source_root, "run-env")], tmp_path / "export", signer)


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(tmp_path / "cli-audit.jsonl"))
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
    return home


def _import(export_dir: Path, keys: tuple[Path, Path], *extra: str) -> None:
    result = runner.invoke(
        app, ["import", str(export_dir), "--public-key", str(keys[1]), "--no-reindex", *extra]
    )
    assert result.exit_code == 0, (result.exception, result.output)


def test_env_store_is_the_default_target(
    home: Path, export_dir: Path, keys: tuple[Path, Path], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "data" / "capsules"
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(store))
    _import(export_dir, keys)
    assert (store / "run-env").is_dir()
    assert not (home / "capsules" / "run-env").exists()


def test_home_capsules_without_env(
    home: Path, export_dir: Path, keys: tuple[Path, Path]
) -> None:
    _import(export_dir, keys)
    assert (home / "capsules" / "run-env").is_dir()


def test_explicit_flag_wins(
    home: Path, export_dir: Path, keys: tuple[Path, Path], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(tmp_path / "data" / "capsules"))
    flag = tmp_path / "flag"
    _import(export_dir, keys, "--capsule-dir", str(flag))
    assert (flag / "run-env").is_dir()
    assert not (tmp_path / "data" / "capsules" / "run-env").exists()
