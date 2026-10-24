"""Ctrl-C (or a forwarded SIGTERM) during a remote capture cancels the job (review capture-runners-03).

The SLURM / LSF / PBS / Docker / Kubernetes runners poll a job the scheduler runs
elsewhere. An interruption propagated out of the poll loop and left the job queued
or running — on the cluster's allocation, writing into a capsule nobody would
finalise. The runner now cancels the job, and once the scheduler reports it
stopped attaches what it collected to the *same* interruption, which it re-raises,
so the orchestrator finalises the capsule as ``aborted``. A job it could not stop
gets nothing attached: sealing a capsule a live job may still write is worse.

Each case fakes the scheduler with a ``subprocess.run`` that answers by argv[0].
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from novafabric.runners import (
    DockerRunner,
    KubernetesRunner,
    LSFRunner,
    PBSRunner,
    RunnerJobSpec,
    SlurmRunner,
)
from novafabric.runners._types import interrupted_result

RUN_ID = "01TESTINTERRUPT000000000000"


def _spec(tmp_path: Path, **options: object) -> RunnerJobSpec:
    cap = tmp_path / "capsule"
    cap.mkdir(parents=True, exist_ok=True)
    return RunnerJobSpec(
        run_id=RUN_ID,
        command=["python", "agent.py"],
        capsule_dir=cap,
        env={"NOVAFABRIC_CAPSULE_DIR": str(cap), "PATH": "/usr/bin"},
        timeout_s=3600.0,
        runner_options={"poll_interval_s": 5.0, **options},
    )


class _Scheduler:
    """Running until cancelled, then terminal; records every call."""

    def __init__(
        self, *, submit: bytes, running: bytes, stopped: bytes, cancel_bin: str,
        cancel_fails: bool = False, stops_after: int = 0,
    ) -> None:
        self.submit, self.running, self.stopped = submit, running, stopped
        self.cancel_bin, self.cancel_fails = cancel_bin, cancel_fails
        self.stops_after = stops_after  # queries after the cancel that still report running
        self.calls: list[list[str]] = []
        self.cancelled = False

    def __call__(self, argv: list[str], *_a: Any, **_k: Any) -> subprocess.CompletedProcess[bytes]:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        tool = Path(argv[0]).name
        if tool in ("sbatch", "bsub", "qsub"):
            return subprocess.CompletedProcess(argv, 0, self.submit, b"")
        if tool == self.cancel_bin:
            if self.cancel_fails:
                raise subprocess.CalledProcessError(1, argv, b"", b"cancel refused")
            self.cancelled = True
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        stopped = self.cancelled and self.stops_after <= 0
        if self.cancelled:
            self.stops_after -= 1
        return subprocess.CompletedProcess(argv, 0, self.stopped if stopped else self.running, b"")


@contextmanager
def _interrupted_while_polling(exc: BaseException) -> Iterator[list[float]]:
    """The first poll sleep raises *exc* (the user's Ctrl-C); later sleeps return."""
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            raise exc

    with patch("time.sleep", side_effect=sleep):
        yield sleeps


SLURM: dict[str, Any] = {
    "submit": b"4242\n",
    "running": b"JobId=4242 JobState=RUNNING ExitCode=0:0",
    "stopped": b"JobId=4242 JobState=CANCELLED ExitCode=0:15",
    "cancel_bin": "scancel",
}


def test_ctrl_c_scancels_the_slurm_job_and_attaches_the_result(tmp_path: Path) -> None:
    fake = _Scheduler(**SLURM)
    with patch("subprocess.run", side_effect=fake), _interrupted_while_polling(
        KeyboardInterrupt()
    ), pytest.raises(KeyboardInterrupt) as exc_info:
        SlurmRunner().run(_spec(tmp_path, partition="gpu"))
    assert ["scancel", "4242"] in fake.calls
    result = interrupted_result(exc_info.value)
    assert result is not None
    assert (result.exit_code, result.runner_status) == (130, "interrupted")
    assert result.runner_metadata["job_id"] == "4242"
    assert result.runner_metadata["cancelled"] is True
    assert result.runner_metadata["final_state_after_cancel"] == "CANCELLED"


def test_settle_polls_after_an_interrupt_are_short(tmp_path: Path) -> None:
    """The user asked to stop: waiting for the job to leave the queue does not use
    the 5 s poll cadence."""
    fake = _Scheduler(**SLURM, stops_after=2)
    with patch("subprocess.run", side_effect=fake), _interrupted_while_polling(
        KeyboardInterrupt()
    ) as sleeps, pytest.raises(KeyboardInterrupt) as exc_info:
        SlurmRunner().run(_spec(tmp_path, partition="gpu"))
    assert interrupted_result(exc_info.value) is not None
    settle = sleeps[1:]
    assert len(settle) == 2 and all(s <= 1.5 for s in settle), sleeps


def test_a_forwarded_sigterm_bkills_the_lsf_job_with_its_exit_code(tmp_path: Path) -> None:
    fake = _Scheduler(
        submit=b"Job <77> is submitted to queue <normal>.\n",
        running=b"77 user RUN normal host host job Oct 24 10:00\n",
        stopped=b"77 user EXIT normal host host job Oct 24 10:00\n",
        cancel_bin="bkill",
    )
    with patch("subprocess.run", side_effect=fake), _interrupted_while_polling(
        SystemExit(143)
    ), pytest.raises(SystemExit) as exc_info:
        LSFRunner().run(_spec(tmp_path))
    assert exc_info.value.code == 143
    assert ["bkill", "77"] in fake.calls
    result = interrupted_result(exc_info.value)
    assert result is not None and result.exit_code == 143


def test_ctrl_c_qdels_the_pbs_job(tmp_path: Path) -> None:
    fake = _Scheduler(
        submit=b"88.server\n",
        running=b"Job Id: 88.server\n    job_state = R\n",
        stopped=b"Job Id: 88.server\n    job_state = F\n    Exit_status = 271\n",
        cancel_bin="qdel",
    )
    with patch("subprocess.run", side_effect=fake), _interrupted_while_polling(
        KeyboardInterrupt()
    ), pytest.raises(KeyboardInterrupt) as exc_info:
        PBSRunner().run(_spec(tmp_path))
    assert ["qdel", "88.server"] in fake.calls
    assert interrupted_result(exc_info.value) is not None


def test_a_job_that_could_not_be_cancelled_gets_no_result(tmp_path: Path) -> None:
    """It may still be running and writing: the capsule must not be finalised."""
    fake = _Scheduler(**SLURM, cancel_fails=True)
    with patch("subprocess.run", side_effect=fake), _interrupted_while_polling(
        KeyboardInterrupt()
    ), pytest.raises(KeyboardInterrupt) as exc_info:
        SlurmRunner().run(_spec(tmp_path, partition="gpu"))
    assert ["scancel", "4242"] in fake.calls
    assert interrupted_result(exc_info.value) is None


def test_a_job_still_running_after_the_cancel_gets_no_result(tmp_path: Path) -> None:
    fake = _Scheduler(**{**SLURM, "stopped": SLURM["running"]})
    with patch("subprocess.run", side_effect=fake), _interrupted_while_polling(
        KeyboardInterrupt()
    ), pytest.raises(KeyboardInterrupt) as exc_info:
        SlurmRunner().run(_spec(tmp_path, partition="gpu"))
    assert interrupted_result(exc_info.value) is None


def test_ctrl_c_removes_the_docker_container(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake(argv: list[str], *_a: Any, **_k: Any) -> subprocess.CompletedProcess[bytes]:
        argv = [str(a) for a in argv]
        calls.append(argv)
        if argv[1:2] == ["run"]:
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(argv, 0, b"[]", b"")

    with patch("subprocess.run", side_effect=fake), pytest.raises(KeyboardInterrupt) as exc_info:
        DockerRunner().run(_spec(tmp_path, image="agent:1"))
    (run_argv,) = [c for c in calls if c[1:2] == ["run"]]
    name = run_argv[run_argv.index("--name") + 1]
    assert ["docker", "rm", "-f", name] in calls
    result = interrupted_result(exc_info.value)
    assert result is not None and result.runner_metadata["cancelled"] is True


def test_ctrl_c_deletes_the_kubernetes_job(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake(argv: list[str], *_a: Any, **_k: Any) -> subprocess.CompletedProcess[bytes]:
        argv = [str(a) for a in argv]
        calls.append(argv)
        if argv[1:3] == ["get", "job"]:
            return subprocess.CompletedProcess(argv, 0, b'{"status": {"active": 1}}', b"")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    with patch("subprocess.run", side_effect=fake), _interrupted_while_polling(
        KeyboardInterrupt()
    ), pytest.raises(KeyboardInterrupt) as exc_info:
        KubernetesRunner().run(_spec(tmp_path, image="agent:1", namespace="ci"))
    deletes = [c for c in calls if c[1:3] == ["delete", "job"]]
    assert deletes, calls
    result = interrupted_result(exc_info.value)
    assert result is not None and result.runner_metadata["cancelled"] is True
