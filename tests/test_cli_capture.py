import sys
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def test_capture_exit_zero(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["capture", "--output-dir", str(tmp_path / "runs"), sys.executable, "-c", "print('ok')"],
    )
    assert result.exit_code == 0
    assert "Capsule written" in result.output


def test_capture_creates_capsule_dir(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    runner.invoke(
        app,
        ["capture", "--output-dir", str(runs_dir), sys.executable, "-c", "pass"],
    )
    assert runs_dir.exists()
    run_dirs = list(runs_dir.iterdir())
    assert len(run_dirs) == 1
    assert (run_dirs[0] / "capsule.yaml").exists()


def test_capture_exit_nonzero_propagates(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["capture", "--output-dir", str(tmp_path / "runs"),
         sys.executable, "-c", "import sys; sys.exit(2)"],
    )
    assert result.exit_code == 2


def test_capture_run_id_in_output(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["capture", "--output-dir", str(tmp_path / "runs"), sys.executable, "-c", "pass"],
    )
    assert "run_id=" in result.output


def test_capture_failure_shown(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["capture", "--output-dir", str(tmp_path / "runs"),
         sys.executable, "-c", "raise SystemExit(1)"],
    )
    assert result.exit_code == 1
