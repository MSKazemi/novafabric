"""Unit tests for PBSRunner (ADR-0025 Runners.5).

All subprocess calls are mocked — these tests pass without a live PBS
cluster or qsub/qstat/qdel on PATH.  Live-cluster validation is a
separate acceptance gate (see ROADMAP).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from novafabric.runners import PBSRunner, RunnerJobSpec, RunnerSpec
from novafabric.runners._pbs import _seconds_to_hms

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _spec(
    tmp_path: Path,
    command: list[str],
    **kwargs: object,
) -> RunnerJobSpec:
    capsule_dir = tmp_path / "capsule"
    capsule_dir.mkdir(parents=True, exist_ok=True)
    return RunnerJobSpec(
        run_id="01TESTPBS000000000000000000",
        command=command,
        capsule_dir=capsule_dir,
        env={
            "NOVAFABRIC_CAPSULE_DIR": str(capsule_dir),
            "NOVAFABRIC_SPAN_ID": "0" * 16,
            "PATH": "/usr/bin",
        },
        **kwargs,  # type: ignore[arg-type]
    )


def _qstat_output(state: str, exit_status: str = "") -> str:
    """Build a minimal ``qstat -f`` output block."""
    lines = [
        "Job Id: 12345.headnode",
        "    job_state = " + state,
    ]
    if exit_status:
        lines.append("    exit_status = " + exit_status)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestPBSRunnerProtocol:
    def test_satisfies_runner_spec(self) -> None:
        runner: RunnerSpec = PBSRunner()
        assert runner.name == "pbs"
        assert "pbs" in runner.description.lower() or "PBS" in runner.description

    def test_name_and_description_are_class_attributes(self) -> None:
        assert PBSRunner.name == "pbs"
        assert isinstance(PBSRunner.description, str)
        assert len(PBSRunner.description) > 0


# ---------------------------------------------------------------------------
# supports()
# ---------------------------------------------------------------------------

class TestPBSRunnerSupports:
    def test_returns_false_when_qsub_not_on_path(self) -> None:
        with patch("shutil.which", side_effect=lambda name: None if name == "qsub" else f"/usr/bin/{name}"):
            ok, why = PBSRunner().supports()
        assert ok is False
        assert "qsub" in why

    def test_returns_false_when_qstat_not_on_path(self) -> None:
        with patch("shutil.which", side_effect=lambda name: None if name == "qstat" else f"/usr/bin/{name}"):
            ok, why = PBSRunner().supports()
        assert ok is False
        assert "qstat" in why

    def test_returns_false_when_qdel_not_on_path(self) -> None:
        with patch("shutil.which", side_effect=lambda name: None if name == "qdel" else f"/usr/bin/{name}"):
            ok, why = PBSRunner().supports()
        assert ok is False
        assert "qdel" in why

    def test_returns_true_when_all_tools_present(self) -> None:
        version_ok = subprocess.CompletedProcess([], 0, b"pbs_version = 20.0.3\n", b"")
        with patch("shutil.which", return_value="/usr/bin/qsub"), \
             patch("subprocess.run", return_value=version_ok):
            ok, why = PBSRunner().supports()
        assert ok is True
        assert why == ""

    def test_returns_false_when_qsub_version_oserror(self) -> None:
        with patch("shutil.which", return_value="/usr/bin/qsub"), \
             patch("subprocess.run", side_effect=OSError("exec error")):
            ok, why = PBSRunner().supports()
        assert ok is False
        assert "qsub" in why


# ---------------------------------------------------------------------------
# submit (via run) — success path
# ---------------------------------------------------------------------------

class TestPBSRunnerSubmit:
    def test_submit_parses_job_id_with_hostname(self, tmp_path: Path) -> None:
        qsub_out = subprocess.CompletedProcess([], 0, b"12345.headnode.example.com\n", b"")
        qstat_completed = subprocess.CompletedProcess(
            [], 0,
            _qstat_output("C", "0").encode(),
            b"",
        )
        with patch("subprocess.run", side_effect=[qsub_out, qstat_completed]):
            result = PBSRunner().run(_spec(tmp_path, ["python", "train.py"], runner_options={"queue": "gpu"}))
        assert result.runner_metadata["job_id"] == "12345.headnode.example.com"
        assert result.runner_status == "completed"

    def test_submit_parses_bare_numeric_job_id(self, tmp_path: Path) -> None:
        qsub_out = subprocess.CompletedProcess([], 0, b"99\n", b"")
        qstat_completed = subprocess.CompletedProcess(
            [], 0,
            _qstat_output("C", "0").encode(),
            b"",
        )
        with patch("subprocess.run", side_effect=[qsub_out, qstat_completed]):
            result = PBSRunner().run(_spec(tmp_path, ["echo", "hi"]))
        assert "99" in result.runner_metadata["job_id"]

    def test_qsub_failure_returns_failed_setup(self, tmp_path: Path) -> None:
        qsub_fail = subprocess.CompletedProcess(
            [], 1, b"", b"qsub: Unknown queue 'no-such-queue'\n",
        )
        with patch("subprocess.run", return_value=qsub_fail):
            result = PBSRunner().run(_spec(tmp_path, ["echo"]))
        assert result.runner_status == "failed_setup"
        assert result.exit_code == 125

    def test_qsub_not_available_returns_failed_setup(self, tmp_path: Path) -> None:
        with patch("subprocess.run", side_effect=OSError("qsub: not found")):
            result = PBSRunner().run(_spec(tmp_path, ["echo"]))
        assert result.runner_status == "failed_setup"
        assert result.exit_code == 127
        assert "PBS" in (result.runner_error or "") or "qsub" in (result.runner_error or "")

    def test_unparseable_job_id_returns_failed_setup(self, tmp_path: Path) -> None:
        qsub_garbage = subprocess.CompletedProcess([], 0, b"garbled output\n", b"")
        with patch("subprocess.run", return_value=qsub_garbage):
            result = PBSRunner().run(_spec(tmp_path, ["echo"]))
        assert result.runner_status == "failed_setup"
        assert "job ID" in (result.runner_error or "")


# ---------------------------------------------------------------------------
# wait / polling
# ---------------------------------------------------------------------------

class TestPBSRunnerPolling:
    def test_wait_polls_until_completed(self, tmp_path: Path) -> None:
        """qstat returns Q on first call, then C on second."""
        qsub_out = subprocess.CompletedProcess([], 0, b"12345\n", b"")
        qstat_running = subprocess.CompletedProcess(
            [], 0, _qstat_output("Q").encode(), b"",
        )
        qstat_done = subprocess.CompletedProcess(
            [], 0, _qstat_output("C", "0").encode(), b"",
        )
        with patch("subprocess.run", side_effect=[qsub_out, qstat_running, qstat_done]), \
             patch("time.sleep"):
            result = PBSRunner().run(
                _spec(tmp_path, ["echo"], runner_options={"poll_interval_s": 0.01})
            )
        assert result.runner_status == "completed"
        assert result.runner_metadata["final_state"] == "C"

    def test_running_state_is_not_terminal(self, tmp_path: Path) -> None:
        """E (Exiting) is not terminal — runner keeps polling."""
        qsub_out = subprocess.CompletedProcess([], 0, b"42\n", b"")
        qstat_exiting = subprocess.CompletedProcess(
            [], 0, _qstat_output("E").encode(), b"",
        )
        qstat_completed = subprocess.CompletedProcess(
            [], 0, _qstat_output("C", "0").encode(), b"",
        )
        with patch("subprocess.run", side_effect=[qsub_out, qstat_exiting, qstat_completed]), \
             patch("time.sleep"):
            result = PBSRunner().run(
                _spec(tmp_path, ["echo"], runner_options={"poll_interval_s": 0.01})
            )
        assert result.runner_status == "completed"

    def test_timeout_returns_timeout_status(self, tmp_path: Path) -> None:
        """When deadline passes before terminal state, runner_status is 'timeout'."""
        qsub_out = subprocess.CompletedProcess([], 0, b"77\n", b"")
        qstat_running = subprocess.CompletedProcess(
            [], 0, _qstat_output("R").encode(), b"",
        )
        # Exhaust the timeout by always returning R.
        with patch("subprocess.run", side_effect=[qsub_out] + [qstat_running] * 10), \
             patch("time.sleep"), \
             patch("time.monotonic", side_effect=[0.0, 999.0, 999.0, 999.0]):
            result = PBSRunner().run(
                _spec(tmp_path, ["echo"], timeout_s=1.0)
            )
        assert result.runner_status == "timeout"
        assert result.exit_code == 124

    def test_failed_job_sets_nonzero_exit_code(self, tmp_path: Path) -> None:
        """A job that exits with non-zero (exit_status != 0) should propagate."""
        qsub_out = subprocess.CompletedProcess([], 0, b"55\n", b"")
        qstat_failed = subprocess.CompletedProcess(
            [], 0, _qstat_output("C", "42").encode(), b"",
        )
        with patch("subprocess.run", side_effect=[qsub_out, qstat_failed]):
            result = PBSRunner().run(_spec(tmp_path, ["false"]))
        assert result.exit_code == 42
        assert result.runner_metadata["final_state"] == "C"


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------

class TestPBSRunnerCancel:
    def test_cancel_calls_qdel(self) -> None:
        qdel_ok = subprocess.CompletedProcess([], 0, b"", b"")
        with patch("subprocess.run", return_value=qdel_ok) as mock_run:
            PBSRunner().cancel("12345.headnode")
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "qdel"
        assert "12345.headnode" in args

    def test_cancel_with_custom_qdel_bin(self) -> None:
        qdel_ok = subprocess.CompletedProcess([], 0, b"", b"")
        with patch("subprocess.run", return_value=qdel_ok) as mock_run:
            PBSRunner(qdel_bin="/opt/pbs/bin/qdel").cancel("99")
        args = mock_run.call_args[0][0]
        assert args[0] == "/opt/pbs/bin/qdel"


# ---------------------------------------------------------------------------
# sitecustomize injection
# ---------------------------------------------------------------------------

class TestPBSRunnerSitecustomize:
    def test_sitecustomize_written_to_capsule_dir(self, tmp_path: Path) -> None:
        qsub_out = subprocess.CompletedProcess([], 0, b"1\n", b"")
        qstat_done = subprocess.CompletedProcess(
            [], 0, _qstat_output("C", "0").encode(), b"",
        )
        spec = _spec(tmp_path, ["echo", "hi"])
        with patch("subprocess.run", side_effect=[qsub_out, qstat_done]):
            PBSRunner().run(spec)
        sitecustomize = spec.capsule_dir / "sitecustomize.py"
        assert sitecustomize.exists(), (
            "PBSRunner did not materialize sitecustomize.py in capsule_dir"
        )
        text = sitecustomize.read_text()
        assert "novafabric.capture.hooks" in text
        assert "install_all" in text

    def test_pbs_script_contains_pythonpath_injection(self, tmp_path: Path) -> None:
        qsub_out = subprocess.CompletedProcess([], 0, b"1\n", b"")
        qstat_done = subprocess.CompletedProcess(
            [], 0, _qstat_output("C", "0").encode(), b"",
        )
        spec = _spec(tmp_path, ["echo"])
        with patch("subprocess.run", side_effect=[qsub_out, qstat_done]):
            PBSRunner().run(spec)
        script_files = list(spec.capsule_dir.glob("*.pbs"))
        assert script_files, "No .pbs script found in capsule_dir"
        script_text = script_files[0].read_text()
        assert "PYTHONPATH" in script_text
        assert str(spec.capsule_dir) in script_text


# ---------------------------------------------------------------------------
# walltime helpers
# ---------------------------------------------------------------------------

class TestPBSWalltimeHelper:
    def test_zero_seconds(self) -> None:
        assert _seconds_to_hms(0) == "00:00:00"

    def test_one_hour(self) -> None:
        assert _seconds_to_hms(3600) == "01:00:00"

    def test_90_minutes(self) -> None:
        assert _seconds_to_hms(5400) == "01:30:00"

    def test_derived_from_timeout_s(self, tmp_path: Path) -> None:
        """When walltime is not explicit, it is derived from timeout_s."""
        qsub_out = subprocess.CompletedProcess([], 0, b"1\n", b"")
        qstat_done = subprocess.CompletedProcess(
            [], 0, _qstat_output("C", "0").encode(), b"",
        )
        spec = _spec(tmp_path, ["echo"], timeout_s=7200.0)
        with patch("subprocess.run", side_effect=[qsub_out, qstat_done]):
            PBSRunner().run(spec)
        script_files = list(spec.capsule_dir.glob("*.pbs"))
        assert script_files
        script_text = script_files[0].read_text()
        # 7200s = 2h = 02:00:00
        assert "02:00:00" in script_text


# ---------------------------------------------------------------------------
# _parse_status unit tests
# ---------------------------------------------------------------------------

class TestPBSParseStatus:
    def test_parse_running_state(self) -> None:
        state, exit_status = PBSRunner()._parse_status(_qstat_output("R"))
        assert state == "R"
        assert exit_status == ""

    def test_parse_completed_state_with_exit(self) -> None:
        state, exit_status = PBSRunner()._parse_status(_qstat_output("C", "0"))
        assert state == "C"
        assert exit_status == "0"

    def test_parse_queued_state(self) -> None:
        state, _ = PBSRunner()._parse_status(_qstat_output("Q"))
        assert state == "Q"

    def test_parse_held_state(self) -> None:
        state, _ = PBSRunner()._parse_status(_qstat_output("H"))
        assert state == "H"

    def test_parse_empty_output_returns_none(self) -> None:
        state, exit_status = PBSRunner()._parse_status("")
        assert state is None
        assert exit_status == ""


# ---------------------------------------------------------------------------
# registry integration
# ---------------------------------------------------------------------------

class TestPBSRunnerRegistry:
    def test_pbs_in_known_runner_names(self) -> None:
        from novafabric.runners import known_runner_names
        assert "pbs" in known_runner_names()

    def test_get_runner_returns_pbs_runner(self) -> None:
        from novafabric.runners import get_runner
        runner = get_runner("pbs")
        assert isinstance(runner, PBSRunner)
