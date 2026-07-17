"""Watermark-based new-run detection for the dashboard SSE feed.

The stats-refresh loop needs "which runs appeared since the last tick?".
Loading the full ``runs_cache`` and diffing run_id sets is O(total runs) per
tick and silently breaks once the index outgrows the fetch limit, so this
tracker keeps a ``(created_at, run_ids-at-that-timestamp)`` high-water mark
and asks the index only for rows at or after it. Per-tick cost is bounded by
the number of rows sharing or exceeding the newest timestamp — normally the
handful of runs created between polls.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from novafabric.registry.runs_cache import query_runs

__all__ = ["NewRunTracker"]


class NewRunTracker:
    """Stateful detector: each :meth:`poll` returns rows not seen before.

    The first poll only establishes the baseline (nothing is "new" at
    startup). ``batch_limit`` bounds a single poll's fetch; it caps runs
    detected *per tick*, not the total index size the tracker can serve.
    Rows without a ``created_at`` cannot be ordered against the watermark
    and are ignored after the baseline poll.
    """

    def __init__(self, batch_limit: int = 10_000) -> None:
        self._batch_limit = batch_limit
        self._primed = False
        self._watermark: str | None = None
        self._at_watermark: set[str] = set()

    def poll(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        """Return newly indexed run rows, oldest first ([] on the first call)."""
        rows, _total = query_runs(
            conn, limit=self._batch_limit, since=self._watermark
        )
        # query_runs orders created_at DESC, run_id DESC.
        if not self._primed:
            self._primed = True
            self._advance(rows)
            return []

        if self._watermark is None:
            new = [r for r in rows if r.get("created_at") is not None]
        else:
            new = [
                r
                for r in rows
                if r.get("created_at") is not None
                and (
                    r["created_at"] > self._watermark
                    or (
                        r["created_at"] == self._watermark
                        and r["run_id"] not in self._at_watermark
                    )
                )
            ]
        self._advance(rows)
        return list(reversed(new))

    def _advance(self, rows: list[dict[str, Any]]) -> None:
        """Move the high-water mark to the newest timestamp in *rows*."""
        dated = [r for r in rows if r.get("created_at") is not None]
        if not dated:
            return
        top = dated[0]["created_at"]
        ids_at_top = {r["run_id"] for r in dated if r["created_at"] == top}
        if top == self._watermark:
            self._at_watermark |= ids_at_top
        else:
            self._watermark = top
            self._at_watermark = ids_at_top
