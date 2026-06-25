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
