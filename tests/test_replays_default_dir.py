"""L29: replays default to ``$NOVAFABRIC_HOME/replays``, never the current directory.

``nova init`` creates ``$NOVAFABRIC_HOME/replays`` but nothing used it: every
replay surface defaulted to ``./.novafabric/replays`` under the CWD, so a replay
run from a repository checkout wrote its capsule into the checkout. The default
now comes from one helper, :func:`novafabric._paths.default_replays_dir`, like
capture's :func:`~novafabric._paths.default_capsule_dir`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from novafabric import _paths
from novafabric.cli.main import app

SRC = Path(__file__).resolve().parent.parent / "src" / "novafabric"
runner = CliRunner()


def _make_capsule(root: Path) -> Path:
    cap = root / "test-capsule"
    (cap / "inputs").mkdir(parents=True)
    (cap / "outputs").mkdir()
    (cap / "capsule.yaml").write_text(yaml.dump({
        "schema_version": "0.1.0",
        "run_id": "TESTREPLAYL29",
        "status": "success",
        "command": ["python", "-c", "print('hi')"],
        "capture_mode": "cli-wrapper",
        "novafabric_version": "0.2.0",
    }))
    (cap / "env.lock").write_text(yaml.dump({
        "python": {"version": "3.12.3", "interpreter": "cpython"},
        "host": {"os": "linux", "arch": "x86_64"},
    }))
    (cap / "replay.yaml").write_text(yaml.dump({"schema_version": "0.1.0"}))
    for name in ("model-calls.jsonl", "tool-calls.jsonl", "trace.jsonl"):
        (cap / name).write_text("")
    (cap / "redaction-proof.json").write_text(json.dumps({"findings": []}))
    return cap


@pytest.fixture
def home_and_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "nova-home"
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.chdir(cwd)
    return home, cwd


def test_default_replays_dir_is_under_nova_home(home_and_cwd: tuple[Path, Path]) -> None:
    home, _ = home_and_cwd
    assert _paths.default_replays_dir() == home / "replays"


def test_nova_init_creates_the_directory_replays_use(home_and_cwd: tuple[Path, Path]) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert _paths.default_replays_dir().is_dir()


def test_nova_replay_without_output_dir_writes_under_nova_home(
    home_and_cwd: tuple[Path, Path], tmp_path: Path
) -> None:
    home, cwd = home_and_cwd
    cap = _make_capsule(tmp_path)
    result = runner.invoke(app, ["replay", str(cap), "--mode", "forensic"])
    assert isinstance(result.exception, SystemExit) or result.exception is None
    assert result.exit_code == 0, result.output
    assert not (cwd / ".novafabric").exists(), "replay written into the current directory"
    written = [p for p in (home / "replays").iterdir() if p.is_dir()]
    assert len(written) == 1
    assert (written[0] / "replay_result.yaml").is_file()


def test_replay_engine_default_base_dir_is_under_nova_home(
    home_and_cwd: tuple[Path, Path], tmp_path: Path
) -> None:
    from novafabric.replay import ReplayEngine, ReplayFlags

    home, cwd = home_and_cwd
    engine = ReplayEngine(capsule_dir=_make_capsule(tmp_path), flags=ReplayFlags(mode="forensic"))
    result = engine.run()
    assert (home / "replays" / result.replay_id).is_dir()
    assert not (cwd / ".novafabric").exists()


#: Any spelling of a CWD-relative replays default in the package source.
_CWD_REPLAYS = re.compile(r"""Path\.cwd\(\)\s*/\s*["']\.novafabric["']\s*/\s*["']replays["']""")


def test_no_replay_surface_defaults_to_the_current_directory() -> None:
    """One definition: ``nova replay``, attest-replay, session replay and diagnose."""
    offenders = [
        f"{path.relative_to(SRC)}:{n}"
        for path in SRC.rglob("*.py")
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _CWD_REPLAYS.search(line)
    ]
    assert offenders == []
