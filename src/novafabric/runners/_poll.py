"""Jittered polling sleep for scheduler runners.

Many concurrent runners polling the same scheduler API (Slurm ``sacct``, the
Kubernetes API, LSF ``bjobs``, PBS ``qstat``) on a fixed interval synchronise
into a thundering herd — every poll lands at the same instant and hammers the
control plane. Adding a small random jitter to each sleep decorrelates the
pollers without changing the average cadence.

The randomness is for load-spreading only, not security.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Collection
from typing import Any

logger = logging.getLogger(__name__)

#: Polls after a cancel, to see the job leave the queue before the capsule is sealed.
CANCEL_SETTLE_POLLS = 6

# Fractional jitter applied to each poll interval (±15%).
_JITTER = 0.15


def jittered_sleep(poll_interval: float, *, jitter: float = _JITTER) -> None:
    """Sleep for ``poll_interval`` seconds ± up to ``jitter`` fraction.

    E.g. a 5 s interval with the default 0.15 jitter sleeps a uniformly-random
    duration in [4.25, 5.75] s. The mean stays at ``poll_interval``.
    """
    if poll_interval <= 0:
        return
    factor = 1.0 + random.uniform(-jitter, jitter)  # noqa: S311 — load-spread, not crypto
    time.sleep(poll_interval * factor)


def cancel_after_deadline(
    job_id: str,
    *,
    cancel: Callable[[str], None],
    query: Callable[[str], str | None],
    terminal: Collection[str],
    poll_interval: float,
) -> dict[str, Any]:
    """Cancel a job its runner's deadline gave up on; return what happened, for ``runner_metadata``.

    A timed-out job used to be abandoned: still queued or running on the shared
    filesystem, it went on writing into the capsule after it was sealed (review
    capture-runners-04). Fail-open — a cancel that fails is recorded
    (``cancelled: false`` + ``cancel_error``), never raised. After a successful
    cancel the state is polled up to :data:`CANCEL_SETTLE_POLLS` times (a bounded
    count, not a clock) until the scheduler reports a terminal state.
    """
    try:
        cancel(job_id)
    except Exception as exc:  # noqa: BLE001 — the timeout result must still be returned
        logger.warning("could not cancel job %s after its deadline: %s", job_id, exc)
        return {"cancelled": False, "cancel_error": f"{type(exc).__name__}: {exc}"[:300]}
    state: str | None = None
    for _ in range(CANCEL_SETTLE_POLLS):
        try:
            state = query(job_id)
        except Exception:  # noqa: BLE001 — the cancel itself went through
            break
        if state in terminal:
            break
        jittered_sleep(poll_interval)
    return {"cancelled": True, "final_state_after_cancel": state}
