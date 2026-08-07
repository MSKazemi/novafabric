"""Durable background jobs (ADR-0242).

One place where "this work was accepted" is written *before* the work starts,
so a restart, deploy, or crash can never silently lose accepted work. The
store is SQLite in local/serve mode (the Postgres/RLS backend is the next
slice); workers claim jobs by **atomic lease**, not check-then-act — crash
recovery is a property of the data model, not a recovery procedure.
"""

from novafabric.jobs.models import Job, JobState
from novafabric.jobs.runner import JobHandler, JobRunner
from novafabric.jobs.store import (
    JobNotFoundError,
    JobStore,
    StaleWorkerError,
    default_jobs_db_path,
)

__all__ = [
    "Job",
    "JobHandler",
    "JobNotFoundError",
    "JobRunner",
    "JobState",
    "JobStore",
    "StaleWorkerError",
    "default_jobs_db_path",
]
