"""When a runner's deadline passes, the job is cancelled, not abandoned (review capture-runners-04).

The runner used to return ``124`` / ``timeout`` and leave the SLURM / LSF / PBS job
queued or running (``cancel()`` existed for LSF and PBS but nothing called it;
SLURM had no ``scancel`` at all), and for Docker only the ``docker`` client was
killed — the container ran on. The orchestrator then sealed the capsule while the
job could still append to ``model-calls.jsonl`` and its log files, so the capsule
later failed ``nova verify``, and the allocation was spent on an abandoned run.

Each case fakes the scheduler with a ``subprocess.run`` that answers by argv[0] and
records every call.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

from novafabric.runners import DockerRunner, LSFRunner, PBSRunner, RunnerJobSpec, SlurmRunner

RUN_ID = "01TESTTIMEOUT0000000000000A"


def _spec(tmp_path: Path, **options: object) -> RunnerJobSpec:
    cap = tmp_path / "capsule"
    cap.mkdir(parents=True, exist_ok=True)
    return RunnerJobSpec(
        run_id=RUN_ID,
        command=["python", "agent.py"],
        capsule_dir=cap,
        env={"NOVAFABRIC_CAPSULE_DIR": str(cap), "PATH": "/usr/bin"},
        timeout_s=1.0,
        runner_options={"poll_interval_s": 0.01, **options},
    )


class _Scheduler:
    """A fake scheduler: running until cancelled, then terminal."""

    def __init__(self, *, submit: bytes, running: bytes, stopped: bytes, cancel_bin: str) -> None:
        self.submit, self.running, self.stopped, self.cancel_bin = submit, running, stopped, cancel_bin
        self.calls: list[list[str]] = []
        self.cancelled = False

    def __call__(self, argv: list[str], *_a: Any, **_k: Any) -> subprocess.CompletedProcess[bytes]:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        tool = Path(argv[0]).name
        if tool in ("sbatch", "bsub", "qsub"):
            return subprocess.CompletedProcess(argv, 0, self.submit, b"")
        if tool == self.cancel_bin:
            self.cancelled = True
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return subprocess.CompletedProcess(argv, 0, self.stopped if self.cancelled else self.running, b"")


def _deadline_passes() -> Any:
    clock = iter([0.0] + [999.0] * 1000)
    return patch("time.monotonic", side_effect=lambda: next(clock))


def test_slurm_job_is_scancelled_when_the_deadline_passes(tmp_path: Path) -> None:
    fake = _Scheduler(
        submit=b"4242\n",
        running=b"JobId=4242 JobState=RUNNING ExitCode=0:0",
        stopped=b"JobId=4242 JobState=CANCELLED ExitCode=0:15",
        cancel_bin="scancel",
    )
    with patch("subprocess.run", side_effect=fake), patch("time.sleep"), _deadline_passes():
        result = SlurmRunner().run(_spec(tmp_path, partition="gpu"))
    assert result.runner_status == "timeout" and result.exit_code == 124
    assert ["scancel", "4242"] in fake.calls
    assert result.runner_metadata["cancelled"] is True
    assert result.runner_metadata["final_state_after_cancel"] == "CANCELLED"


def test_lsf_job_is_bkilled_when_the_deadline_passes(tmp_path: Path) -> None:
    fake = _Scheduler(
        submit=b"Job <77> is submitted to queue <normal>.\n",
        running=b"77 user RUN normal host host job Oct 24 10:00\n",
        stopped=b"77 user EXIT normal host host job Oct 24 10:00\n",
        cancel_bin="bkill",
    )
    with patch("subprocess.run", side_effect=fake), patch("time.sleep"), _deadline_passes():
        result = LSFRunner().run(_spec(tmp_path))
    assert result.runner_status == "timeout"
    assert ["bkill", "77"] in fake.calls
    assert result.runner_metadata["cancelled"] is True
    assert result.runner_metadata["final_state_after_cancel"] == "EXIT"


def test_pbs_job_is_qdeleted_when_the_deadline_passes(tmp_path: Path) -> None:
    fake = _Scheduler(
        submit=b"88.server\n",
        running=b"Job Id: 88.server\n    job_state = R\n",
        stopped=b"Job Id: 88.server\n    job_state = F\n    Exit_status = 271\n",
        cancel_bin="qdel",
    )
    with patch("subprocess.run", side_effect=fake), patch("time.sleep"), _deadline_passes():
        result = PBSRunner().run(_spec(tmp_path))
    assert result.runner_status == "timeout"
    assert ["qdel", "88.server"] in fake.calls
    assert result.runner_metadata["cancelled"] is True


def test_a_cancel_that_fails_is_recorded_not_raised(tmp_path: Path) -> None:
    fake = _Scheduler(
        submit=b"77\n", running=b"JobId=77 JobState=RUNNING", stopped=b"", cancel_bin="scancel",
    )

    def failing(argv: list[str], *a: Any, **k: Any) -> subprocess.CompletedProcess[bytes]:
        if Path(str(argv[0])).name == "scancel":
            raise subprocess.CalledProcessError(1, argv, b"", b"scancel: error: Kill job error")
        return fake(argv, *a, **k)

    with patch("subprocess.run", side_effect=failing), patch("time.sleep"), _deadline_passes():
        result = SlurmRunner().run(_spec(tmp_path, partition="gpu"))
    assert result.runner_status == "timeout"
    assert result.runner_metadata["cancelled"] is False
    assert "cancel_error" in result.runner_metadata


def test_docker_container_is_removed_when_the_deadline_passes(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake(argv: list[str], *_a: Any, **_k: Any) -> subprocess.CompletedProcess[bytes]:
        argv = [str(a) for a in argv]
        calls.append(argv)
        if argv[1:2] == ["run"]:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=1.0)
        return subprocess.CompletedProcess(argv, 0, b"[]", b"")

    with patch("subprocess.run", side_effect=fake):
        result = DockerRunner().run(_spec(tmp_path, image="agent:1"))
    assert result.runner_status == "timeout"
    (run_argv,) = [c for c in calls if c[1:2] == ["run"]]
    name = run_argv[run_argv.index("--name") + 1]
    assert RUN_ID.lower() in name
    assert ["docker", "rm", "-f", name] in calls
    assert result.runner_metadata["cancelled"] is True
