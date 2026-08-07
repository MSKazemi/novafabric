"""Replays resource — /v0/replays.

Implements:
  POST   /v0/replays                         schedule a replay job
  GET    /v0/replays/{replay_id}             get status
  DELETE /v0/replays/{replay_id}             cancel
  GET    /v0/replays/{replay_id}/events      SSE stream of replay progress

Job *state* is durable (ADR-0242: the shared :class:`~novafabric.jobs.JobStore`
under ``$NOVAFABRIC_HOME/jobs.db``), so a restart or a different ``--workers``
process reports correct status instead of 404 — the pre-ADR in-memory
``_JOBS`` dict lost every job on restart and was invisible to sibling
workers. SSE *progress events* remain in-process detail: after a restart the
stream replays no history, but the terminal event is still emitted from
durable state. Replay jobs are single-attempt (re-running a half-finished
replay automatically is not wanted); an interrupted job therefore reads
``failed`` with an explicit interruption error — visible, never silent.
"""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from novafabric.jobs import Job, JobNotFoundError, JobState, JobStore, StaleWorkerError
from novafabric.server.auth import AuthContext
from novafabric.server.deps import get_capsule_dir, get_db_path
from novafabric.server.errors import BadRequestError, ConflictError, NotFoundError
from novafabric.server.rbac import Role, require_role

router = APIRouter(prefix="/replays", tags=["replays"])

_KIND = "replay"

# Progress events are in-process, bounded per job; the durable store carries
# state. One lock guards the event lists.
_EVENTS: dict[str, list[dict[str, Any]]] = {}
_EVENTS_LOCK = threading.Lock()
_MAX_EVENTS_PER_JOB = 1000

# Path-keyed cache: NOVAFABRIC_HOME can differ per process configuration (and
# per test); the store for a given db path is constructed once and recovery
# (expired leases → visible failed state) runs on that first construction.
_stores: dict[Path, JobStore] = {}
_store_lock = threading.Lock()


def _jobs() -> JobStore:
    from novafabric.jobs.store import default_jobs_db_path

    path = default_jobs_db_path()
    with _store_lock:
        store = _stores.get(path)
        if store is None:
            store = JobStore(path)
            store.expire_leases()
            _stores[path] = store
        return store


def _get_job(replay_id: str) -> Job:
    try:
        job = _jobs().get(replay_id)
    except JobNotFoundError:
        raise NotFoundError(f"Replay '{replay_id}' not found.") from None
    if job.kind != _KIND:
        raise NotFoundError(f"Replay '{replay_id}' not found.")
    return job


# ---------- schedule ----------


@router.post("", status_code=202, response_model=None)
async def schedule_replay(
    body: dict[str, Any],
    capsule_dir: Annotated[Path, Depends(get_capsule_dir)] = None,  # type: ignore[assignment]
    db_path: Annotated[Path | None, Depends(get_db_path)] = None,
    _auth: Annotated[AuthContext, Depends(require_role(Role.writer))] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    run_id = body.get("run_id")
    mode = body.get("mode")
    if not run_id:
        raise BadRequestError("run_id is required")
    if not mode:
        raise BadRequestError("mode is required")
    valid_modes = {"forensic", "mocked", "shadow", "dry_run"}
    if mode not in valid_modes:
        raise BadRequestError(f"mode must be one of {sorted(valid_modes)}")

    # Verify the capsule exists
    capsule_path = capsule_dir / run_id
    if not capsule_path.is_dir() or not (capsule_path / "capsule.yaml").exists():
        raise NotFoundError(f"Capsule '{run_id}' not found.")

    replay_id = str(uuid.uuid4())
    store = _jobs()
    store.enqueue(
        _KIND,
        {"run_id": run_id, "mode": mode, "capsule_dir": str(capsule_path)},
        max_attempts=1,
        job_id=replay_id,
    )
    with _EVENTS_LOCK:
        _EVENTS[replay_id] = []

    # Claim immediately and execute in a background thread: the request-scoped
    # runner keeps the pre-ADR execution model while the state is durable.
    worker_id = f"replays-{uuid.uuid4().hex[:8]}"
    claimed = store.claim(worker_id, kinds=(_KIND,), lease_seconds=3600.0)
    if claimed is not None and claimed.job_id == replay_id:
        threading.Thread(
            target=_run_replay_job,
            args=(replay_id, worker_id, capsule_path, mode),
            daemon=True,
        ).start()

    return _job_summary(_get_job(replay_id))


# ---------- get ----------


@router.get("/{replay_id}", response_model=None)
async def get_replay(
    replay_id: str,
    _auth: Annotated[AuthContext, Depends(require_role(Role.reader))] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return _job_summary(_get_job(replay_id))


# ---------- cancel ----------


@router.delete("/{replay_id}", response_model=None)
async def cancel_replay(
    replay_id: str,
    _auth: Annotated[AuthContext, Depends(require_role(Role.writer))] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    job = _get_job(replay_id)
    if job.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
        raise ConflictError(
            f"Replay '{replay_id}' is already in terminal state "
            f"'{_STATE_TO_API[job.state]}'."
        )
    _jobs().cancel(replay_id)
    return _job_summary(_get_job(replay_id))


# ---------- SSE events ----------


@router.get("/{replay_id}/events", response_model=None)
async def replay_events(  # pragma: no cover — SSE streaming; tested in integration tests
    replay_id: str,
    last_event_id: str | None = Query(default=None, alias="Last-Event-ID"),
    _auth: Annotated[AuthContext, Depends(require_role(Role.reader))] = None,  # type: ignore[assignment]
) -> StreamingResponse:
    _get_job(replay_id)  # validates existence

    async def event_generator() -> Any:
        from_idx = 0
        if last_event_id is not None:
            try:
                from_idx = int(last_event_id) + 1
            except ValueError:
                from_idx = 0

        max_polls = 300  # safety bound (~30 s at 100 ms intervals)
        import asyncio

        polls = 0
        while polls < max_polls:
            try:
                job = _jobs().get(replay_id)
            except JobNotFoundError:
                break

            with _EVENTS_LOCK:
                events = list(_EVENTS.get(replay_id, ()))
            for i, event in enumerate(events[from_idx:], start=from_idx):
                data = json.dumps(event)
                yield f"id: {i}\ndata: {data}\n\n"
                from_idx = i + 1

            if job.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
                terminal_data = json.dumps(
                    {"event": _STATE_TO_API[job.state], "replay_id": replay_id}
                )
                yield f"data: {terminal_data}\n\n"
                break

            await asyncio.sleep(0.1)
            polls += 1

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------- background job runner ----------


def _push_event(replay_id: str, event_type: str, data: dict[str, Any]) -> None:
    with _EVENTS_LOCK:
        events = _EVENTS.setdefault(replay_id, [])
        if len(events) < _MAX_EVENTS_PER_JOB:
            events.append({"event": event_type, **data})


def _run_replay_job(
    replay_id: str, worker_id: str, capsule_path: Path, mode: str
) -> None:
    """Execute the replay; terminal state goes through the guarded store write,
    so a job cancelled meanwhile keeps ``cancelled`` and this thread's late
    result is dropped (StaleWorkerError) instead of resurrecting the job."""

    _push_event(replay_id, "progress", {"message": f"Starting {mode} replay…"})

    try:
        from novafabric.replay._engine import ReplayEngine
        from novafabric.replay._flags import ReplayFlags

        flags = ReplayFlags(mode=mode)  # type: ignore[arg-type]
        engine = ReplayEngine(
            capsule_dir=capsule_path,
            flags=flags,
            base_dir=capsule_path.parent,
        )
        result = engine.run()
        if hasattr(result, "model_dump"):
            report = result.model_dump(mode="json")
        else:  # pragma: no cover
            report = dict(result.__dict__)

        _push_event(replay_id, "progress", {"message": "Replay complete.", "result": report})
        try:
            _jobs().finish(replay_id, worker_id, {"report": report})
        except StaleWorkerError:  # cancelled meanwhile — keep cancelled
            return
        _push_event(replay_id, "completed", {"replay_id": replay_id, "result": report})

    except Exception as exc:  # noqa: BLE001  # pragma: no cover
        err_msg = str(exc)
        _push_event(replay_id, "error", {"message": err_msg})
        try:
            _jobs().fail(replay_id, worker_id, err_msg)
        except StaleWorkerError:
            return


# ---------- helpers ----------

# Public API vocabulary is unchanged from the pre-ADR contract.
_STATE_TO_API = {
    JobState.QUEUED: "pending",
    JobState.RUNNING: "running",
    JobState.SUCCEEDED: "completed",
    JobState.FAILED: "failed",
    JobState.CANCELLED: "cancelled",
}


def _job_summary(job: Job) -> dict[str, Any]:
    return {
        "replay_id": job.job_id,
        "run_id": job.payload.get("run_id"),
        "mode": job.payload.get("mode"),
        "status": _STATE_TO_API[job.state],
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "error": job.error,
    }
