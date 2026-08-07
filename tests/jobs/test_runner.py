"""JobRunner behavior (ADR-0242 D3/D4): bounded pool, handler contract,
bounded retries, cooperative cancel, clean stop."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from novafabric.jobs import Job, JobHandler, JobRunner, JobState, JobStore

# Timing-sensitive (poller cadence + thread pool under xdist load): keep the
# module on one worker so CPU starvation cannot stretch the margins.
pytestmark = pytest.mark.xdist_group("jobs-runner")


@pytest.fixture()
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs.db")


def _wait_for(predicate, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached in time")


def test_handler_must_declare_idempotency() -> None:
    with pytest.raises(ValueError, match="already_done"):
        JobHandler(kind="k", fn=lambda job: None)
    JobHandler(kind="k", fn=lambda job: None, idempotent=True)  # ok
    JobHandler(kind="k", fn=lambda job: None, already_done=lambda job: False)  # ok


def test_runner_executes_and_records_result(store: JobStore) -> None:
    def handler(job: Job) -> dict:
        return {"doubled": job.payload["n"] * 2}

    runner = JobRunner(
        store,
        [JobHandler(kind="double", fn=handler, idempotent=True)],
        workers=2,
        poll_interval=0.05,
    )
    job = store.enqueue("double", {"n": 21})
    runner.start()
    try:
        _wait_for(lambda: store.get(job.job_id).state is JobState.SUCCEEDED)
    finally:
        runner.stop()
    got = store.get(job.job_id)
    assert got.result == {"doubled": 42}


def test_already_done_guard_short_circuits(store: JobStore) -> None:
    calls: list[str] = []

    runner = JobRunner(
        store,
        [
            JobHandler(
                kind="guarded",
                fn=lambda job: calls.append(job.job_id),  # type: ignore[arg-type,return-value]
                already_done=lambda job: True,
            )
        ],
        poll_interval=0.05,
    )
    job = store.enqueue("guarded")
    runner.start()
    try:
        _wait_for(lambda: store.get(job.job_id).state is JobState.SUCCEEDED)
    finally:
        runner.stop()
    assert calls == []
    assert store.get(job.job_id).result == {"already_done": True}


def test_failures_retry_bounded_then_fail(store: JobStore) -> None:
    attempts: list[int] = []

    def flaky(job: Job) -> dict:
        attempts.append(job.attempt)
        raise RuntimeError(f"boom on attempt {job.attempt}")

    runner = JobRunner(
        store,
        [JobHandler(kind="flaky", fn=flaky, idempotent=True)],
        poll_interval=0.05,
    )
    job = store.enqueue("flaky", max_attempts=3)
    runner.start()
    try:
        _wait_for(lambda: store.get(job.job_id).state is JobState.FAILED)
        # No further attempts after terminal failure.
        time.sleep(0.2)
    finally:
        runner.stop()
    assert attempts == [1, 2, 3]
    got = store.get(job.job_id)
    assert got.error is not None and "boom on attempt 3" in got.error


def test_cancel_requested_drops_late_result(store: JobStore) -> None:
    release = threading.Event()

    def slow(job: Job) -> dict:
        release.wait(10.0)
        return {"finished": True}

    runner = JobRunner(
        store,
        [JobHandler(kind="slow", fn=slow, idempotent=True)],
        poll_interval=0.05,
    )
    job = store.enqueue("slow")
    runner.start()
    try:
        _wait_for(lambda: store.get(job.job_id).state is JobState.RUNNING)
        store.cancel(job.job_id)
        release.set()
        time.sleep(0.3)  # let the handler's late finish be refused
    finally:
        runner.stop()
    got = store.get(job.job_id)
    assert got.state is JobState.CANCELLED
    assert got.result is None, "a cancelled job must not gain a late result"


def test_stop_drains_and_double_start_refused(store: JobStore) -> None:
    runner = JobRunner(
        store, [JobHandler(kind="x", fn=lambda j: None, idempotent=True)]
    )
    runner.start()
    with pytest.raises(RuntimeError):
        runner.start()
    runner.stop()
    runner.start()  # restartable after stop
    runner.stop()
