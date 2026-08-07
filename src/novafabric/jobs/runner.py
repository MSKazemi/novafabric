"""Bounded in-process job runner (ADR-0242 D3/D4).

A ``ThreadPoolExecutor`` of fixed size polls the store, claims by lease,
heartbeats while running, and applies the per-kind handler contract:

- a handler must declare whether it is naturally **idempotent** or supplies
  an ``already_done`` guard — a kind cannot register without declaring which
  (the lease is the lock; the guard is only the idempotency backstop);
- retries are bounded by the job's ``max_attempts`` — never an unbounded loop;
- a handler exception marks the attempt failed (requeue happens via lease
  bookkeeping on the terminal write, visibly, with the error recorded).

No broker, no new process type: callers start/stop the runner from an app
lifespan or a CLI command.
"""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from novafabric.jobs.models import Job
from novafabric.jobs.store import JobStore, StaleWorkerError

logger = logging.getLogger(__name__)

HandlerFn = Callable[[Job], "dict[str, Any] | None"]
AlreadyDoneFn = Callable[[Job], bool]


@dataclass(frozen=True)
class JobHandler:
    """Registration record for one job kind."""

    kind: str
    fn: HandlerFn
    idempotent: bool = False
    already_done: AlreadyDoneFn | None = None

    def __post_init__(self) -> None:
        if not self.idempotent and self.already_done is None:
            raise ValueError(
                f"handler for kind {self.kind!r} must either declare "
                "idempotent=True or supply an already_done() guard (ADR-0242 D3)"
            )


class JobRunner:
    """Claim → run → finish loop over a :class:`JobStore`."""

    def __init__(
        self,
        store: JobStore,
        handlers: list[JobHandler],
        *,
        workers: int = 4,
        poll_interval: float = 1.0,
        lease_seconds: float = 30.0,
    ) -> None:
        if workers < 1:
            raise ValueError("workers must be >= 1")
        self._store = store
        self._handlers = {h.kind: h for h in handlers}
        if len(self._handlers) != len(handlers):
            raise ValueError("duplicate handler kind")
        self._workers = workers
        self._poll_interval = poll_interval
        self._lease_seconds = lease_seconds
        self._worker_id = f"runner-{uuid.uuid4().hex[:12]}"
        self._stop = threading.Event()
        self._inflight: set[str] = set()
        self._inflight_lock = threading.Lock()
        self._pool: ThreadPoolExecutor | None = None
        self._loop_thread: threading.Thread | None = None

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def start(self) -> None:
        if self._loop_thread is not None:
            raise RuntimeError("runner already started")
        self._pool = ThreadPoolExecutor(
            max_workers=self._workers, thread_name_prefix="nova-jobs"
        )
        self._loop_thread = threading.Thread(
            target=self._loop, name="nova-jobs-poller", daemon=True
        )
        self._loop_thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=timeout)
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
        self._loop_thread = None
        self._pool = None

    # ---------- internals ----------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001 — the poller must survive anything
                logger.exception("job runner tick failed")
            self._stop.wait(self._poll_interval)

    def _tick(self) -> None:
        recovered = self._store.expire_leases()
        for job in recovered:
            logger.warning(
                "job %s (%s) lease expired — now %s (attempt %d/%d)",
                job.job_id, job.kind, job.state.value, job.attempt, job.max_attempts,
            )

        with self._inflight_lock:
            for job_id in list(self._inflight):
                self._store.heartbeat(
                    job_id, self._worker_id, lease_seconds=self._lease_seconds
                )
            free = self._workers - len(self._inflight)

        kinds = tuple(self._handlers)
        for _ in range(max(0, free)):
            claimed = self._store.claim(
                self._worker_id, kinds=kinds, lease_seconds=self._lease_seconds
            )
            if claimed is None:
                break
            with self._inflight_lock:
                self._inflight.add(claimed.job_id)
            assert self._pool is not None
            self._pool.submit(self._run_one, claimed)

    def _run_one(self, job: Job) -> None:
        handler = self._handlers[job.kind]
        try:
            if handler.already_done is not None and handler.already_done(job):
                self._store.finish(
                    job.job_id, self._worker_id, {"already_done": True}
                )
                return
            result = handler.fn(job)
            self._store.finish(job.job_id, self._worker_id, result)
        except StaleWorkerError:
            # Cancelled meanwhile, or the lease was lost and reclaimed — the
            # terminal-write guard already dropped this attempt's result.
            logger.warning("job %s: terminal write refused (stale worker)", job.job_id)
        except Exception as exc:  # noqa: BLE001 — handler errors become job state
            try:
                if job.attempt < job.max_attempts:
                    # Record the error, return the job to the queue for a
                    # bounded retry: fail-then-requeue in one guarded step.
                    self._store.fail(job.job_id, self._worker_id, str(exc))
                    self._store.requeue_failed(job.job_id)
                else:
                    self._store.fail(job.job_id, self._worker_id, str(exc))
            except StaleWorkerError:
                logger.warning(
                    "job %s: failure could not be recorded (stale worker)", job.job_id
                )
        finally:
            with self._inflight_lock:
                self._inflight.discard(job.job_id)
