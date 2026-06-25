"""Replays resource — /v0/replays.

Implements:
  POST   /v0/replays                         schedule a replay job
  GET    /v0/replays/{replay_id}             get status
  DELETE /v0/replays/{replay_id}             cancel
  GET    /v0/replays/{replay_id}/events      SSE stream of replay progress
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from novafabric.server.auth import AuthContext
from novafabric.server.deps import get_capsule_dir, get_db_path
from novafabric.server.errors import BadRequestError, ConflictError, NotFoundError
from novafabric.server.rbac import Role, require_role

router = APIRouter(prefix="/replays", tags=["replays"])

# In-process job store — simple dict protected by a lock.
# This is intentionally lightweight: replays are a convenience wrapper over
# the existing CLI replay engine.  Persistent job storage is deferred to a
# later pass.
_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_job(replay_id: str) -> dict[str, Any]:
    with _JOBS_LOCK:
        job = _JOBS.get(replay_id)
    if not job:
        raise NotFoundError(f"Replay '{replay_id}' not found.")
    return dict(job)


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
    job: dict[str, Any] = {
        "replay_id": replay_id,
        "run_id": run_id,
        "mode": mode,
        "status": "pending",
        "created_at": _now(),
        "finished_at": None,
        "error": None,
        "_capsule_dir": str(capsule_path),
        "_events": [],
    }
    with _JOBS_LOCK:
        _JOBS[replay_id] = job

    # Launch replay in a background thread
    thread = threading.Thread(
        target=_run_replay_job,
        args=(replay_id, capsule_path, mode),
        daemon=True,
    )
    thread.start()

    return _job_summary(job)


# ---------- get ----------


@router.get("/{replay_id}", response_model=None)
async def get_replay(replay_id: str) -> dict[str, Any]:
    return _job_summary(_get_job(replay_id))


# ---------- cancel ----------


@router.delete("/{replay_id}", response_model=None)
async def cancel_replay(
    replay_id: str,
    _auth: Annotated[AuthContext, Depends(require_role(Role.writer))] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    job = _get_job(replay_id)
    if job["status"] in ("completed", "failed", "cancelled"):
        raise ConflictError(
            f"Replay '{replay_id}' is already in terminal state '{job['status']}'."
        )
    with _JOBS_LOCK:
        if replay_id in _JOBS:
            _JOBS[replay_id]["status"] = "cancelled"
            _JOBS[replay_id]["finished_at"] = _now()
    return _job_summary(_get_job(replay_id))


# ---------- SSE events ----------


@router.get("/{replay_id}/events", response_model=None)
async def replay_events(  # pragma: no cover — SSE streaming; tested in integration tests
    replay_id: str,
    last_event_id: str | None = Query(default=None, alias="Last-Event-ID"),
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
            with _JOBS_LOCK:
                job = _JOBS.get(replay_id)
            if not job:
                break

            events = job.get("_events", [])
            for i, event in enumerate(events[from_idx:], start=from_idx):
                data = json.dumps(event)
                yield f"id: {i}\ndata: {data}\n\n"
                from_idx = i + 1

            if job["status"] in ("completed", "failed", "cancelled"):
                # Emit a final terminal event
                terminal_data = json.dumps(
                    {"event": job["status"], "replay_id": replay_id}
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


def _run_replay_job(replay_id: str, capsule_path: Path, mode: str) -> None:
    """Execute the replay in a daemon thread; update job state."""

    def _push_event(event_type: str, data: dict[str, Any]) -> None:
        with _JOBS_LOCK:
            if replay_id in _JOBS:
                _JOBS[replay_id]["_events"].append({"event": event_type, **data})

    with _JOBS_LOCK:
        if replay_id in _JOBS:
            _JOBS[replay_id]["status"] = "running"

    _push_event("progress", {"message": f"Starting {mode} replay…"})

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

        _push_event("progress", {"message": "Replay complete.", "result": report})

        with _JOBS_LOCK:
            if replay_id in _JOBS:
                _JOBS[replay_id]["status"] = "completed"
                _JOBS[replay_id]["finished_at"] = _now()

        _push_event("completed", {"replay_id": replay_id, "result": report})

    except Exception as exc:  # noqa: BLE001  # pragma: no cover
        err_msg = str(exc)
        _push_event("error", {"message": err_msg})
        with _JOBS_LOCK:
            if replay_id in _JOBS:
                _JOBS[replay_id]["status"] = "failed"
                _JOBS[replay_id]["finished_at"] = _now()
                _JOBS[replay_id]["error"] = err_msg


# ---------- helpers ----------


def _job_summary(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "replay_id": job["replay_id"],
        "run_id": job["run_id"],
        "mode": job["mode"],
        "status": job["status"],
        "created_at": job["created_at"],
        "finished_at": job["finished_at"],
        "error": job.get("error"),
    }
