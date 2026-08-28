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


class TestWorkloadNeverStartedIsDistinguishable:
    """`nova capture <typo>` must not look identical to a workload that ran and failed.

    Both exit 127 and both write a capsule — deliberately, per the issue — so the
    only thing that can carry the difference is the printed message. The guard
    that matters is the second test: keying the message on ``exit_code == 127``
    instead of on ``runner_status`` would pass the first test and silently
    mislabel every real program that exits 127.
    """

    NOT_A_COMMAND = "definitely-not-a-real-command-xyz"

    def test_mistyped_command_says_the_workload_never_started(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            ["capture", "--output-dir", str(tmp_path / "runs"), self.NOT_A_COMMAND],
        )
        assert "Workload never started" in result.output
        # The two documented invariants are unchanged.
        assert result.exit_code == 127
        assert "Capsule written" in result.output
        run_dirs = list((tmp_path / "runs").iterdir())
        assert len(run_dirs) == 1
        assert (run_dirs[0] / "capsule.yaml").exists()

    def test_real_program_exiting_127_is_not_mislabelled(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            ["capture", "--output-dir", str(tmp_path / "runs"),
             sys.executable, "-c", "import sys; sys.exit(127)"],
        )
        assert result.exit_code == 127
        assert "Capsule written" in result.output
        assert "Workload never started" not in result.output

    def test_the_reason_names_the_command(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            ["capture", "--output-dir", str(tmp_path / "runs"), self.NOT_A_COMMAND],
        )
        # Rich wraps the console at terminal width; join before matching.
        flat = "".join(result.output.split())
        assert self.NOT_A_COMMAND in flat
