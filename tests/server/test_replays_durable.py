"""Replay jobs survive the process (ADR-0242 slice 1, first consumer).

The pre-ADR ``_JOBS`` dict lost every job on restart (GET → 404, work silently
gone) and was invisible to sibling ``--workers`` processes. These tests pin
the new durable behavior: state readable across "restarts" (fresh store over
the same file), interrupted work visible as ``failed`` — never silent — and
the public response vocabulary unchanged.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.jobs import JobState, JobStore  # noqa: E402
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import ServerConfig  # noqa: E402


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    capsules = tmp_path / "capsules"
    capsules.mkdir()
    monkeypatch.setenv("NOVAFABRIC_JOBS_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(capsules))
    cfg = ServerConfig(
        insecure_no_auth=True,
        db_path=str(tmp_path / "registry.db"),
    )
    app = create_app(cfg)
    with TestClient(app) as c:
        yield c


def _make_capsule(capsule_dir: Path, run_id: str) -> None:
    d = capsule_dir / run_id
    d.mkdir(parents=True)
    (d / "capsule.yaml").write_text("schema_version: '0.2'\nrun_id: " + run_id + "\n")


def test_schedule_reports_and_persists(client: TestClient, tmp_path: Path) -> None:
    _make_capsule(tmp_path / "capsules", "run-1")
    resp = client.post("/v0/replays", json={"run_id": "run-1", "mode": "dry_run"})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["run_id"] == "run-1"
    assert body["mode"] == "dry_run"
    assert body["status"] in {"pending", "running", "completed", "failed"}
    replay_id = body["replay_id"]

    # The job is durable: visible directly in the store file, not a dict.
    store = JobStore(tmp_path / "jobs.db")
    job = store.get(replay_id)
    assert job.kind == "replay"
    assert job.payload["run_id"] == "run-1"

    # And the API reads it back (poll to terminal to avoid leaking a thread).
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = client.get(f"/v0/replays/{replay_id}").json()["status"]
        if status in {"completed", "failed"}:
            break
        time.sleep(0.05)
    assert status in {"completed", "failed"}


def test_interrupted_job_is_visible_failed_not_silent(tmp_path: Path, client: TestClient) -> None:
    """Simulated crash: a running single-attempt job whose lease expired reads
    ``failed`` with an explicit interruption error after 'restart' recovery."""
    store = JobStore(tmp_path / "jobs.db")
    job = store.enqueue(
        "replay", {"run_id": "run-x", "mode": "dry_run"}, max_attempts=1
    )
    claimed = store.claim("dead-worker", kinds=("replay",), lease_seconds=-1.0)
    assert claimed is not None and claimed.job_id == job.job_id

    # "Restart": recovery sweep (the route runs this on first store touch).
    JobStore(tmp_path / "jobs.db").expire_leases()

    got = store.get(job.job_id)
    assert got.state is JobState.FAILED
    assert got.error is not None and "worker died or process restarted" in got.error

    resp = client.get(f"/v0/replays/{job.job_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"


def test_cancel_semantics_unchanged(client: TestClient, tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    job = store.enqueue("replay", {"run_id": "r", "mode": "dry_run"}, max_attempts=1)

    resp = client.delete(f"/v0/replays/{job.job_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    # Terminal-state cancel conflicts, as before.
    resp = client.delete(f"/v0/replays/{job.job_id}")
    assert resp.status_code == 409


def test_unknown_replay_404(client: TestClient) -> None:
    assert client.get("/v0/replays/nope").status_code == 404
    assert client.delete("/v0/replays/nope").status_code == 404
