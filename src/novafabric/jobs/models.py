"""Job model and states (ADR-0242 D1)."""

from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class JobState(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset(
    {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}
)


class Job(BaseModel):
    """One accepted unit of background work.

    Timestamps are ISO-8601 UTC strings (the store's wire format);
    ``lease_expires_at`` is a POSIX epoch float because it is compared
    arithmetically on every claim.
    """

    model_config = ConfigDict(frozen=True)

    job_id: str
    kind: str
    tenant_id: str = "default"
    payload: dict[str, Any] = Field(default_factory=dict)
    state: JobState = JobState.QUEUED
    attempt: int = 0
    max_attempts: int = 3
    lease_expires_at: float | None = None
    worker_id: str | None = None
    cancel_requested: bool = False
    created_at: str = ""
    updated_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
