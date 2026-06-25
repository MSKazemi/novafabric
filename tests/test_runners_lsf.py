"""Unit tests for LSFRunner (ADR-0025 Runners.6).

All subprocess calls are mocked — these tests pass without a live LSF
cluster or bsub/bjobs/bkill on PATH.  Live-cluster validation is a
separate acceptance gate (see ROADMAP).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from novafabric.runners import LSFRunner, RunnerJobSpec, RunnerSpec
from novafabric.runners._lsf import _seconds_to_lsf_walltime

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
        run_id="01TESTLSF000000000000000000",
        command=command,
        capsule_dir=capsule_dir,
        env={
            "NOVAFABRIC_CAPSULE_DIR": str(capsule_dir),
            "NOVAFABRIC_SPAN_ID": "0" * 16,
            "PATH": "/usr/bin",
        },
        **kwargs,  # type: ignore[arg-type]
    )


def _bsub_output(job_id: str = "12345", queue: str = "normal") -> bytes:
    """Build a realistic bsub submission line."""
    return f"Job <{job_id}> is submitted to queue <{queue}>.\n".encode()


def _bjobs_output(job_id: str, stat: str) -> bytes:
    """Build a minimal bjobs -noheader output line."""
    return f"{job_id}  user1  {stat}  normal  headnode  exec01  nova-job  May 17 10:00\n".encode()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestLSFRunnerProtocol:
    def test_satisfies_runner_spec(self) -> None:
        runner: RunnerSpec = LSFRunner()
        assert runner.name == "lsf"
        assert "lsf" in runner.description.lower() or "LSF" in runner.description

    def test_name_and_description_are_class_attributes(self) -> None:
        assert LSFRunner.name == "lsf"
        assert isinstance(LSFRunner.description, str)
        assert len(LSFRunner.description) > 0


# ---------------------------------------------------------------------------
# supports()
# ---------------------------------------------------------------------------

class TestLSFRunnerSupports:
    def test_returns_false_when_bsub_not_on_path(self) -> None:
        with patch("shutil.which", side_effect=lambda name: None if name == "bsub" else f"/usr/bin/{name}"):
            ok, why = LSFRunner().supports()
        assert ok is False
        assert "bsub" in why

    def test_returns_false_when_bjobs_not_on_path(self) -> None:
        with patch("shutil.which", side_effect=lambda name: None if name == "bjobs" else f"/usr/bin/{name}"):
            ok, why = LSFRunner().supports()
        assert ok is False
        assert "bjobs" in why

    def test_returns_false_when_bkill_not_on_path(self) -> None:
        with patch("shutil.which", side_effect=lambda name: None if name == "bkill" else f"/usr/bin/{name}"):
            ok, why = LSFRunner().supports()
        assert ok is False
        assert "bkill" in why

    def test_returns_true_when_all_tools_present(self) -> None:
        version_ok = subprocess.CompletedProcess([], 0, b"Platform LSF 10.1\n", b"")
        with patch("shutil.which", return_value="/usr/bin/bsub"), \
             patch("subprocess.run", return_value=version_ok):
            ok, why = LSFRunner().supports()
        assert ok is True
        assert why == ""

    def test_returns_false_when_bsub_oserror(self) -> None:
        with patch("shutil.which", return_value="/usr/bin/bsub"), \
             patch("subprocess.run", side_effect=OSError("exec error")):
            ok, why = LSFRunner().supports()
        assert ok is False
        assert "bsub" in why


# ---------------------------------------------------------------------------
# submit (via run) — success path
# ---------------------------------------------------------------------------

class TestLSFRunnerSubmit:
    def test_submit_parses_job_id(self, tmp_path: Path) -> None:
        bsub_out = subprocess.CompletedProcess([], 0, _bsub_output("12345"), b"")
        bjobs_done = subprocess.CompletedProcess(
            [], 0, _bjobs_output("12345", "DONE"), b""
        )
        with patch("subprocess.run", side_effect=[bsub_out, bjobs_done]):
            result = LSFRunner().run(_spec(tmp_path, ["python", "train.py"]))
        assert result.runner_metadata["job_id"] == "12345"
        assert result.runner_status == "completed"

    def test_submit_uses_queue_option(self, tmp_path: Path) -> None:
        bsub_out = subprocess.CompletedProcess([], 0, _bsub_output("99", "gpu"), b"")
        bjobs_done = subprocess.CompletedProcess(
            [], 0, _bjobs_output("99", "DONE"), b""
        )
        with patch("subprocess.run", side_effect=[bsub_out, bjobs_done]):
            result = LSFRunner().run(
                _spec(tmp_path, ["echo"], runner_options={"queue": "gpu"})
            )
        assert result.runner_metadata["queue"] == "gpu"

    def test_bsub_failure_returns_failed_setup(self, tmp_path: Path) -> None:
        bsub_fail = subprocess.CompletedProcess(
            [], 1, b"", b"bsub: Unknown queue 'no-such-queue'\n",
        )
        with patch("subprocess.run", return_value=bsub_fail):
            result = LSFRunner().run(_spec(tmp_path, ["echo"]))
        assert result.runner_status == "failed_setup"
        assert result.exit_code == 125

    def test_bsub_not_available_returns_failed_setup(self, tmp_path: Path) -> None:
        with patch("subprocess.run", side_effect=OSError("bsub: not found")):
            result = LSFRunner().run(_spec(tmp_path, ["echo"]))
        assert result.runner_status == "failed_setup"
        assert result.exit_code == 127
        assert "LSF" in (result.runner_error or "") or "bsub" in (result.runner_error or "")

    def test_unparseable_job_id_returns_failed_setup(self, tmp_path: Path) -> None:
        bsub_garbage = subprocess.CompletedProcess([], 0, b"Unexpected output\n", b"")
        with patch("subprocess.run", return_value=bsub_garbage):
            result = LSFRunner().run(_spec(tmp_path, ["echo"]))
        assert result.runner_status == "failed_setup"
        assert "job ID" in (result.runner_error or "")

    def test_bsub_timeout_returns_failed_setup(self, tmp_path: Path) -> None:
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("bsub", 30)):
            result = LSFRunner().run(_spec(tmp_path, ["echo"]))
        assert result.runner_status == "failed_setup"
        assert result.exit_code == 127


# ---------------------------------------------------------------------------
# wait / polling
# ---------------------------------------------------------------------------

class TestLSFRunnerPolling:
    def test_wait_polls_until_done(self, tmp_path: Path) -> None:
        """bjobs returns PEND on first call, then DONE on second."""
        bsub_out = subprocess.CompletedProcess([], 0, _bsub_output("10"), b"")
        bjobs_pend = subprocess.CompletedProcess(
            [], 0, _bjobs_output("10", "PEND"), b""
        )
        bjobs_done = subprocess.CompletedProcess(
            [], 0, _bjobs_output("10", "DONE"), b""
        )
        with patch("subprocess.run", side_effect=[bsub_out, bjobs_pend, bjobs_done]), \
             patch("time.sleep"):
            result = LSFRunner().run(
                _spec(tmp_path, ["echo"], runner_options={"poll_interval_s": 0.01})
            )
        assert result.runner_status == "completed"
        assert result.exit_code == 0
        assert result.runner_metadata["final_state"] == "DONE"

    def test_exit_state_sets_nonzero_exit_code(self, tmp_path: Path) -> None:
        """EXIT terminal state → exit_code = 1."""
        bsub_out = subprocess.CompletedProcess([], 0, _bsub_output("20"), b"")
        bjobs_exit = subprocess.CompletedProcess(
            [], 0, _bjobs_output("20", "EXIT"), b""
        )
        with patch("subprocess.run", side_effect=[bsub_out, bjobs_exit]):
            result = LSFRunner().run(_spec(tmp_path, ["false"]))
        assert result.exit_code == 1
        assert result.runner_metadata["final_state"] == "EXIT"

    def test_run_state_is_not_terminal(self, tmp_path: Path) -> None:
        """RUN state is not terminal — runner keeps polling."""
        bsub_out = subprocess.CompletedProcess([], 0, _bsub_output("30"), b"")
        bjobs_run = subprocess.CompletedProcess(
            [], 0, _bjobs_output("30", "RUN"), b""
        )
        bjobs_done = subprocess.CompletedProcess(
            [], 0, _bjobs_output("30", "DONE"), b""
        )
        with patch("subprocess.run", side_effect=[bsub_out, bjobs_run, bjobs_done]), \
             patch("time.sleep"):
            result = LSFRunner().run(
                _spec(tmp_path, ["echo"], runner_options={"poll_interval_s": 0.01})
            )
        assert result.runner_status == "completed"
        assert result.exit_code == 0

    def test_timeout_returns_timeout_status(self, tmp_path: Path) -> None:
        """When deadline passes before terminal state, runner_status is 'timeout'."""
        bsub_out = subprocess.CompletedProcess([], 0, _bsub_output("77"), b"")
        bjobs_run = subprocess.CompletedProcess(
            [], 0, _bjobs_output("77", "RUN"), b""
        )
        with patch("subprocess.run", side_effect=[bsub_out] + [bjobs_run] * 10), \
             patch("time.sleep"), \
             patch("time.monotonic", side_effect=[0.0, 999.0, 999.0, 999.0]):
            result = LSFRunner().run(
                _spec(tmp_path, ["echo"], timeout_s=1.0)
            )
        assert result.runner_status == "timeout"
        assert result.exit_code == 124

    def test_ususp_state_is_not_terminal(self, tmp_path: Path) -> None:
        """USUSP (user-suspended) is not terminal — runner keeps polling."""
        bsub_out = subprocess.CompletedProcess([], 0, _bsub_output("40"), b"")
        bjobs_susp = subprocess.CompletedProcess(
            [], 0, _bjobs_output("40", "USUSP"), b""
        )
        bjobs_done = subprocess.CompletedProcess(
            [], 0, _bjobs_output("40", "DONE"), b""
        )
        with patch("subprocess.run", side_effect=[bsub_out, bjobs_susp, bjobs_done]), \
             patch("time.sleep"):
            result = LSFRunner().run(
                _spec(tmp_path, ["echo"], runner_options={"poll_interval_s": 0.01})
            )
        assert result.runner_status == "completed"


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------

class TestLSFRunnerCancel:
    def test_cancel_calls_bkill(self) -> None:
        bkill_ok = subprocess.CompletedProcess([], 0, b"Job <12345> is being killed\n", b"")
        with patch("subprocess.run", return_value=bkill_ok) as mock_run:
            LSFRunner().cancel("12345")
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "bkill"
        assert "12345" in args

    def test_cancel_with_custom_bkill_bin(self) -> None:
        bkill_ok = subprocess.CompletedProcess([], 0, b"", b"")
        with patch("subprocess.run", return_value=bkill_ok) as mock_run:
            LSFRunner(bkill_bin="/opt/lsf/bin/bkill").cancel("42")
        args = mock_run.call_args[0][0]
        assert args[0] == "/opt/lsf/bin/bkill"


# ---------------------------------------------------------------------------
# sitecustomize injection
# ---------------------------------------------------------------------------

class TestLSFRunnerSitecustomize:
    def test_sitecustomize_written_to_capsule_dir(self, tmp_path: Path) -> None:
        bsub_out = subprocess.CompletedProcess([], 0, _bsub_output("1"), b"")
        bjobs_done = subprocess.CompletedProcess(
            [], 0, _bjobs_output("1", "DONE"), b""
        )
        spec = _spec(tmp_path, ["echo", "hi"])
        with patch("subprocess.run", side_effect=[bsub_out, bjobs_done]):
            LSFRunner().run(spec)
        sitecustomize = spec.capsule_dir / "sitecustomize.py"
        assert sitecustomize.exists(), (
            "LSFRunner did not materialize sitecustomize.py in capsule_dir"
        )
        text = sitecustomize.read_text()
        assert "novafabric.capture.hooks" in text
        assert "install_all" in text

    def test_lsf_script_contains_pythonpath_injection(self, tmp_path: Path) -> None:
        bsub_out = subprocess.CompletedProcess([], 0, _bsub_output("1"), b"")
        bjobs_done = subprocess.CompletedProcess(
            [], 0, _bjobs_output("1", "DONE"), b""
        )
        spec = _spec(tmp_path, ["echo"])
        with patch("subprocess.run", side_effect=[bsub_out, bjobs_done]):
            LSFRunner().run(spec)
        script_files = list(spec.capsule_dir.glob("*.lsf"))
        assert script_files, "No .lsf script found in capsule_dir"
        script_text = script_files[0].read_text()
        assert "PYTHONPATH" in script_text
        assert str(spec.capsule_dir) in script_text

    def test_resource_requirement_in_script(self, tmp_path: Path) -> None:
        bsub_out = subprocess.CompletedProcess([], 0, _bsub_output("1"), b"")
        bjobs_done = subprocess.CompletedProcess(
            [], 0, _bjobs_output("1", "DONE"), b""
        )
        spec = _spec(
            tmp_path, ["echo"],
            runner_options={"resource_requirement": "rusage[mem=4096]"},
        )
        with patch("subprocess.run", side_effect=[bsub_out, bjobs_done]):
            LSFRunner().run(spec)
        script_files = list(spec.capsule_dir.glob("*.lsf"))
        assert script_files
        script_text = script_files[0].read_text()
        assert "rusage[mem=4096]" in script_text
        assert "#BSUB -R" in script_text


# ---------------------------------------------------------------------------
# walltime helpers
# ---------------------------------------------------------------------------

class TestLSFWalltimeHelper:
    def test_one_hour(self) -> None:
        assert _seconds_to_lsf_walltime(3600) == "1:00"

    def test_90_minutes(self) -> None:
        assert _seconds_to_lsf_walltime(5400) == "1:30"

    def test_30_minutes(self) -> None:
        assert _seconds_to_lsf_walltime(1800) == "0:30"

    def test_two_hours(self) -> None:
        assert _seconds_to_lsf_walltime(7200) == "2:00"

    def test_minimum_one_minute(self) -> None:
        # Even 0 seconds becomes 1 minute (ceil).
        result = _seconds_to_lsf_walltime(0)
        assert result == "0:01"

    def test_derived_from_timeout_s(self, tmp_path: Path) -> None:
        """walltime is derived from timeout_s when not explicitly set."""
        bsub_out = subprocess.CompletedProcess([], 0, _bsub_output("1"), b"")
        bjobs_done = subprocess.CompletedProcess(
            [], 0, _bjobs_output("1", "DONE"), b""
        )
        spec = _spec(tmp_path, ["echo"], timeout_s=3600.0)
        with patch("subprocess.run", side_effect=[bsub_out, bjobs_done]):
            LSFRunner().run(spec)
        script_files = list(spec.capsule_dir.glob("*.lsf"))
        assert script_files
        script_text = script_files[0].read_text()
        # 3600s = 1:00 in LSF format
        assert "1:00" in script_text


# ---------------------------------------------------------------------------
# _parse_job_id unit tests
# ---------------------------------------------------------------------------

class TestLSFParseJobId:
    def test_standard_bsub_output(self) -> None:
        output = "Job <12345> is submitted to queue <normal>."
        assert LSFRunner()._parse_job_id(output) == "12345"

    def test_different_job_id(self) -> None:
        output = "Job <99999> is submitted to queue <gpu>."
        assert LSFRunner()._parse_job_id(output) == "99999"

    def test_unparseable_returns_empty_string(self) -> None:
        output = "garbled non-LSF output"
        assert LSFRunner()._parse_job_id(output) == ""

    def test_empty_string_returns_empty(self) -> None:
        assert LSFRunner()._parse_job_id("") == ""


# ---------------------------------------------------------------------------
# _parse_status unit tests
# ---------------------------------------------------------------------------

class TestLSFParseStatus:
    def test_parse_done_state(self) -> None:
        output = _bjobs_output("12345", "DONE").decode()
        state = LSFRunner()._parse_status(output)
        assert state == "DONE"

    def test_parse_pend_state(self) -> None:
        output = _bjobs_output("12345", "PEND").decode()
        state = LSFRunner()._parse_status(output)
        assert state == "PEND"

    def test_parse_run_state(self) -> None:
        output = _bjobs_output("12345", "RUN").decode()
        state = LSFRunner()._parse_status(output)
        assert state == "RUN"

    def test_parse_exit_state(self) -> None:
        output = _bjobs_output("12345", "EXIT").decode()
        state = LSFRunner()._parse_status(output)
        assert state == "EXIT"

    def test_empty_output_returns_none(self) -> None:
        state = LSFRunner()._parse_status("")
        assert state is None

    def test_empty_line_output_returns_none(self) -> None:
        state = LSFRunner()._parse_status("   \n\n  ")
        assert state is None


# ---------------------------------------------------------------------------
# stdout/stderr log file reading
# ---------------------------------------------------------------------------

class TestLSFLogReading:
    def test_reads_stdout_log_by_job_id(self, tmp_path: Path) -> None:
        capsule_dir = tmp_path / "capsule"
        capsule_dir.mkdir()
        (capsule_dir / "stdout.12345.log").write_bytes(b"hello from LSF\n")
        (capsule_dir / "stderr.12345.log").write_bytes(b"")
        stdout, stderr = LSFRunner()._read_log_files(capsule_dir, "12345")
        assert stdout == b"hello from LSF\n"
        assert stderr == b""

    def test_falls_back_to_glob_when_no_direct_match(self, tmp_path: Path) -> None:
        capsule_dir = tmp_path / "capsule"
        capsule_dir.mkdir()
        (capsule_dir / "stdout.99.log").write_bytes(b"fallback content\n")
        stdout, _ = LSFRunner()._read_log_files(capsule_dir, "does-not-exist")
        assert stdout == b"fallback content\n"

    def test_returns_empty_when_no_logs(self, tmp_path: Path) -> None:
        capsule_dir = tmp_path / "capsule"
        capsule_dir.mkdir()
        stdout, stderr = LSFRunner()._read_log_files(capsule_dir, "12345")
        assert stdout == b""
        assert stderr == b""


# ---------------------------------------------------------------------------
# registry integration
# ---------------------------------------------------------------------------

class TestLSFRunnerRegistry:
    def test_lsf_in_known_runner_names(self) -> None:
        from novafabric.runners import known_runner_names
        assert "lsf" in known_runner_names()

    def test_get_runner_returns_lsf_runner(self) -> None:
        from novafabric.runners import get_runner
        runner = get_runner("lsf")
        assert isinstance(runner, LSFRunner)
