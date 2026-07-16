"""Named exception classes for offline trend reports (ADR-0131)."""

from __future__ import annotations


class TrendError(Exception):
    """Base class for all `nova trend` errors (runtime failures, exit 1)."""


class TrendUsageError(TrendError):
    """The trend request itself is invalid (usage error, exit 2).

    Raised before any capsule is read — an unknown metric or group-by, a
    ``--stat`` on a non-latency metric, an unparsable window, or a window
    that would produce an unbounded number of buckets.
    """
