"""Unit tests for the LocalRunner (ADR-0025 Runners.1).

The orchestrator now delegates subprocess execution to a runner. These
tests pin the behavior of the local runner directly, independent of
the orchestrator. The orchestrator-level smoke tests
(`test_smoke_capture_validate.py`, `test_capture_orchestrator.py`)
continue to exercise the full integration; nothing here replaces those.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from novafabric.runners import (
    LocalRunner,
    RunnerJobResult,
    RunnerJobSpec,
    RunnerSpec,
)


def _spec(tmp_path: Path, command: list[str], **kwargs: object) -> RunnerJobSpec:
    capsule_dir = tmp_path / "capsule"
    capsule_dir.mkdir(parents=True, exist_ok=True)
    return RunnerJobSpec(
        run_id="01TESTRUNNER0000000000000000",
        command=command,
        capsule_dir=capsule_dir,
        env={
            "NOVAFABRIC_CAPSULE_DIR": str(capsule_dir),
            "NOVAFABRIC_SPAN_ID": "0" * 16,
            "PATH": os.environ.get("PATH", ""),
        },
        **kwargs,  # type: ignore[arg-type]
    )


class TestLocalRunnerProtocol:
    def test_satisfies_runner_spec(self) -> None:
        # Static structural check — LocalRunner must be a RunnerSpec.
        runner: RunnerSpec = LocalRunner()
        assert runner.name == "local"
        assert runner.description

    def test_supports_returns_true_unconditionally(self) -> None:
        ok, why = LocalRunner().supports()
        assert ok is True
        assert why == ""


class TestLocalRunnerExecution:
    def test_simple_success_returns_completed(self, tmp_path: Path) -> None:
        result = LocalRunner().run(
            _spec(tmp_path, [sys.executable, "-c", "print('hello')"])
        )
        assert isinstance(result, RunnerJobResult)
        assert result.exit_code == 0
        assert result.runner_status == "completed"
        assert b"hello" in result.stdout
        assert result.runner_error is None

    def test_nonzero_exit_propagated(self, tmp_path: Path) -> None:
        result = LocalRunner().run(
            _spec(tmp_path, [sys.executable, "-c", "import sys; sys.exit(42)"])
        )
        assert result.exit_code == 42
        assert result.runner_status == "completed"  # workload completed, just failed

    def test_stderr_captured_separately(self, tmp_path: Path) -> None:
        result = LocalRunner().run(
            _spec(tmp_path, [
                sys.executable, "-c",
                "import sys; print('out'); print('err', file=sys.stderr)",
            ])
        )
        assert b"out" in result.stdout
        assert b"err" in result.stderr
        assert b"err" not in result.stdout
        assert b"out" not in result.stderr

    def test_env_vars_reach_subprocess(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, [
            sys.executable, "-c",
            "import os; print(os.environ['NOVAFABRIC_SPAN_ID'])",
        ])
        result = LocalRunner().run(spec)
        assert b"0000000000000000" in result.stdout

    def test_pythonpath_includes_sitecustomize(self, tmp_path: Path) -> None:
        """The runner must add a tmpdir containing sitecustomize.py to
        PYTHONPATH so capture hooks auto-install in the subprocess."""
        spec = _spec(tmp_path, [
            sys.executable, "-c",
            "import os; print(os.environ.get('PYTHONPATH', '').split(':')[0])",
        ])
        result = LocalRunner().run(spec)
        # First PYTHONPATH entry should be a tmpdir like /tmp/nf_site_...
        first_entry = result.stdout.decode().strip()
        assert "nf_site_" in first_entry, (
            f"expected sitecustomize tmpdir on PYTHONPATH, got {first_entry!r}"
        )


class TestLocalRunnerTimeout:
    def test_timeout_exceeded_returns_124_and_timeout_status(
        self, tmp_path: Path
    ) -> None:
        spec = _spec(tmp_path, [
            sys.executable, "-c", "import time; time.sleep(10)",
        ], timeout_s=0.5)
        result = LocalRunner().run(spec)
        assert result.exit_code == 124
        assert result.runner_status == "timeout"
        assert result.runner_error is not None
        assert "0.5" in result.runner_error

    def test_default_timeout_is_generous_enough_for_real_work(
        self, tmp_path: Path
    ) -> None:
        # Workload finishes well under the 600s default; should NOT time out.
        result = LocalRunner().run(
            _spec(tmp_path, [sys.executable, "-c", "import time; time.sleep(0.1)"])
        )
        assert result.runner_status == "completed"
        assert result.exit_code == 0


class TestLocalRunnerSetupFailure:
    def test_unknown_command_returns_127_failed_setup(
        self, tmp_path: Path
    ) -> None:
        spec = _spec(tmp_path, [
            "/nonexistent/binary/that/cannot/exist",
        ])
        result = LocalRunner().run(spec)
        assert result.exit_code == 127
        assert result.runner_status == "failed_setup"
        assert result.runner_error is not None

    def test_missing_command_reason_names_it_in_plain_words(
        self, tmp_path: Path
    ) -> None:
        result = LocalRunner().run(_spec(tmp_path, ["definitely-not-a-real-command-xyz"]))
        assert result.exit_code == 127
        assert result.runner_status == "failed_setup"
        assert result.runner_error == "command not found: definitely-not-a-real-command-xyz"

    def test_existing_script_with_missing_interpreter_keeps_os_wording(
        self, tmp_path: Path
    ) -> None:
        # argv[0] exists, so "command not found" would misdirect the user;
        # the ENOENT is about the shebang interpreter instead.
        script = tmp_path / "script.sh"
        script.write_text("#!/nonexistent/interpreter-xyz\necho hi\n")
        script.chmod(0o755)
        result = LocalRunner().run(_spec(tmp_path, [str(script)]))
        assert result.exit_code == 127
        assert result.runner_status == "failed_setup"
        assert result.runner_error is not None
        assert "command not found" not in result.runner_error


# --- review capture-runners-03: an interrupted capture never orphans its workload ---------

import signal  # noqa: E402
import subprocess  # noqa: E402
import textwrap  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

_DRIVER = textwrap.dedent(
    """
    import os, sys
    from pathlib import Path
    from novafabric.runners import _local
    from novafabric.runners import LocalRunner, RunnerJobSpec
    _local.INTERRUPT_GRACE_S = float(sys.argv[3])
    cap = Path(sys.argv[1]); cap.mkdir(parents=True, exist_ok=True)
    LocalRunner().run(RunnerJobSpec(
        run_id="01TESTRUNNER0000000000000000",
        command=["sh", "-c", sys.argv[2]],
        capsule_dir=cap,
        env={"PATH": os.environ["PATH"], "NOVAFABRIC_CAPSULE_DIR": str(cap)},
        timeout_s=120,
    ))
    """
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _start(tmp_path: Path, workload: str, grace: float) -> tuple[subprocess.Popen[bytes], int]:
    pidfile = tmp_path / "workload.pid"
    driver = subprocess.Popen(
        [sys.executable, "-c", _DRIVER, str(tmp_path / "cap"), workload.format(pid=pidfile), str(grace)],
    )
    deadline = time.monotonic() + 30
    while not (pidfile.exists() and pidfile.read_text().strip()):
        assert time.monotonic() < deadline, "workload never started"
        assert driver.poll() is None, "driver exited early"
        time.sleep(0.05)
    return driver, int(pidfile.read_text())


def _wait_dead(pid: int, within: float) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
class TestInterruptedCaptureStopsTheWorkload:
    def test_sigterm_to_nova_capture_is_forwarded_to_the_workload(self, tmp_path: Path) -> None:
        driver, pid = _start(tmp_path, "echo $$ > {pid}; exec sleep 60", grace=10)
        driver.send_signal(signal.SIGTERM)
        assert driver.wait(timeout=30) == 128 + signal.SIGTERM
        assert _wait_dead(pid, within=10), "workload kept running after nova capture was killed"

    def test_a_workload_that_ignores_sigterm_is_killed_after_the_grace_period(
        self, tmp_path: Path
    ) -> None:
        driver, pid = _start(tmp_path, "trap '' TERM; echo $$ > {pid}; while :; do sleep 0.1; done", grace=1)
        driver.send_signal(signal.SIGTERM)
        assert driver.wait(timeout=30) == 128 + signal.SIGTERM
        assert _wait_dead(pid, within=10)

    def test_ctrl_c_stops_the_workload_and_still_interrupts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from novafabric.runners import _local

        pidfile = tmp_path / "pid"
        real_wait = subprocess.Popen.wait
        calls: list[object] = []

        # The first wait is the runner waiting for the workload's exit: Ctrl-C there.
        # Later waits (the runner stopping the group) behave normally.
        def interrupted(self: subprocess.Popen[bytes], *a: Any, **k: Any) -> Any:
            calls.append(k.get("timeout"))
            if len(calls) > 1:
                return real_wait(self, *a, **k)
            deadline = time.monotonic() + 30
            while not (pidfile.exists() and pidfile.read_text().strip()):
                assert time.monotonic() < deadline
                time.sleep(0.05)
            raise KeyboardInterrupt

        monkeypatch.setattr(_local, "INTERRUPT_GRACE_S", 5.0)
        monkeypatch.setattr(subprocess.Popen, "wait", interrupted)
        with pytest.raises(KeyboardInterrupt):
            LocalRunner().run(_spec(tmp_path, ["sh", "-c", f"echo $$ > {pidfile}; exec sleep 60"]))
        monkeypatch.setattr(subprocess.Popen, "wait", real_wait)
        assert _wait_dead(int(pidfile.read_text()), within=10)
