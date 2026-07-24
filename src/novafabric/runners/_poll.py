"""Jittered polling sleep for scheduler runners.

Many concurrent runners polling the same scheduler API (Slurm ``sacct``, the
Kubernetes API, LSF ``bjobs``, PBS ``qstat``) on a fixed interval synchronise
into a thundering herd — every poll lands at the same instant and hammers the
control plane. Adding a small random jitter to each sleep decorrelates the
pollers without changing the average cadence.

The randomness is for load-spreading only, not security.
"""

from __future__ import annotations

import random
import time

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
