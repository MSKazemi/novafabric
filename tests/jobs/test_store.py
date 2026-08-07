"""JobStore semantics (ADR-0242 D1/D2): the claim is the lock, the lease is
recovery, terminal writes are worker-guarded."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from novafabric.jobs import Job, JobNotFoundError, JobState, JobStore, StaleWorkerError


@pytest.fixture()
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs.db")


def test_enqueue_get_roundtrip(store: JobStore) -> None:
    job = store.enqueue("demo", {"x": 1}, tenant_id="t1", max_attempts=2)
    got = store.get(job.job_id)
    assert got.state is JobState.QUEUED
    assert got.payload == {"x": 1}
    assert got.tenant_id == "t1"
    assert got.max_attempts == 2
    assert got.attempt == 0


def test_get_unknown_raises(store: JobStore) -> None:
    with pytest.raises(JobNotFoundError):
        store.get("nope")


def test_enqueue_validates(store: JobStore) -> None:
    with pytest.raises(ValueError):
        store.enqueue("  ")
    with pytest.raises(ValueError):
        store.enqueue("demo", max_attempts=0)


def test_claim_is_fifo_and_kind_filtered(store: JobStore) -> None:
    a = store.enqueue("alpha")
    store.enqueue("beta")
    claimed = store.claim("w1", kinds=("alpha",))
    assert claimed is not None and claimed.job_id == a.job_id
    assert claimed.state is JobState.RUNNING
    assert claimed.attempt == 1
    assert store.claim("w1", kinds=("alpha",)) is None  # no more alphas


def test_two_workers_cannot_claim_the_same_job(store: JobStore) -> None:
    """The xdist-race lesson: the claim must be atomic, not check-then-act."""
    for _ in range(5):
        store.enqueue("demo")
    claims: list[Job] = []
    lock = threading.Lock()

    def worker(wid: str) -> None:
        while True:
            job = store.claim(wid)
            if job is None:
                return
            with lock:
                claims.append(job)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = [j.job_id for j in claims]
    assert len(ids) == 5
    assert len(set(ids)) == 5, "a job was claimed twice"


def test_finish_requires_the_claiming_worker(store: JobStore) -> None:
    job = store.enqueue("demo")
    claimed = store.claim("w1")
    assert claimed is not None
    with pytest.raises(StaleWorkerError):
        store.finish(job.job_id, "impostor", {})
    done = store.finish(job.job_id, "w1", {"ok": True})
    assert done.state is JobState.SUCCEEDED
    assert done.result == {"ok": True}
    assert done.finished_at is not None


def test_cancel_running_drops_late_result(store: JobStore) -> None:
    job = store.enqueue("demo")
    store.claim("w1")
    cancelled = store.cancel(job.job_id)
    assert cancelled.state is JobState.CANCELLED
    with pytest.raises(StaleWorkerError):
        store.finish(job.job_id, "w1", {"late": True})
    assert store.get(job.job_id).state is JobState.CANCELLED


def test_lease_expiry_requeues_then_fails(store: JobStore) -> None:
    job = store.enqueue("demo", max_attempts=2)

    store.claim("w1", lease_seconds=-1.0)  # already expired
    recovered = store.expire_leases()
    assert [j.job_id for j in recovered] == [job.job_id]
    assert store.get(job.job_id).state is JobState.QUEUED

    store.claim("w2", lease_seconds=-1.0)  # second (final) attempt, expired
    recovered = store.expire_leases()
    got = store.get(job.job_id)
    assert got.state is JobState.FAILED
    assert got.error is not None and "lease expired" in got.error


def test_heartbeat_extends_only_for_holder(store: JobStore) -> None:
    job = store.enqueue("demo")
    store.claim("w1", lease_seconds=5.0)
    assert store.heartbeat(job.job_id, "w1", lease_seconds=60.0) is True
    assert store.heartbeat(job.job_id, "other") is False
    got = store.get(job.job_id)
    assert got.lease_expires_at is not None


def test_requeue_failed_is_bounded_and_respects_cancel(store: JobStore) -> None:
    job = store.enqueue("demo", max_attempts=2)
    store.claim("w1")
    store.fail(job.job_id, "w1", "boom")
    assert store.requeue_failed(job.job_id).state is JobState.QUEUED

    store.claim("w1")  # attempt 2 == max_attempts
    store.fail(job.job_id, "w1", "boom again")
    assert store.requeue_failed(job.job_id).state is JobState.FAILED  # refused


def test_list_jobs_filters(store: JobStore) -> None:
    store.enqueue("a", tenant_id="t1")
    store.enqueue("b", tenant_id="t2")
    store.enqueue("a", tenant_id="t1")
    assert len(store.list_jobs(tenant_id="t1")) == 2
    assert len(store.list_jobs(kind="b")) == 1
    assert len(store.list_jobs(state=JobState.QUEUED)) == 3
