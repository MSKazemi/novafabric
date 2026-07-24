"""Bounded retry policy for the NovaFabric Python client (ADR-0202 D7).

Applies only to idempotent requests (every GET; no POST is ever auto-retried
in P1). Bounded exponential backoff with full jitter; a ``Retry-After``
header (seconds or HTTP-date) overrides the computed backoff, capped at 30 s.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

_RETRY_AFTER_CAP_S = 30.0


@dataclass(frozen=True)
class RetryConfig:
    """Retry policy knobs (spec defaults). ``max_attempts=1`` disables retrying.

    ``jitter="full"`` (the normative default) draws the delay uniformly from
    ``[0, computed]``; ``"none"`` uses the computed delay verbatim (useful for
    deterministic tests).
    """

    max_attempts: int = 3
    backoff_base: float = 0.5
    backoff_factor: float = 2.0
    backoff_max: float = 8.0
    jitter: str = "full"
    retry_statuses: tuple[int, ...] = (429, 502, 503, 504)


def compute_backoff(config: RetryConfig, retry_index: int) -> float:
    """Delay before retry *retry_index* (0-based): capped exponential + jitter."""
    delay = min(
        config.backoff_max,
        config.backoff_base * config.backoff_factor**retry_index,
    )
    if config.jitter == "full":
        return random.uniform(0.0, delay)
    return delay


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header (delta-seconds or HTTP-date).

    Returns seconds clamped to ``[0, 30]``, or ``None`` when absent/unparseable.
    """
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    return min(_RETRY_AFTER_CAP_S, max(0.0, seconds))


def _sleep(seconds: float) -> None:
    """Indirection point so tests can observe/skip real sleeping."""
    if seconds > 0:
        time.sleep(seconds)
