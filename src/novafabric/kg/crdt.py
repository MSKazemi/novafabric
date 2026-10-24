"""CRDT G-Counter for edge weight accumulation across distributed collectors.

The GCounter is a grow-only counter (per the standard CRDT definition):
- Merge is elementwise max.
- call_count and verified_count are monotonically non-decreasing.

Invariant: call_count >= verified_count >= 0 after any sequence of
increment() and merge() operations.

WAL-backed CRDTAccumulator
--------------------------
When a *wal_path* is supplied to CRDTAccumulator the accumulator writes every
increment to a write-ahead log (WAL) file before updating its in-memory state.
On restart after a crash the WAL is replayed to recover all increments that
preceded the last ``FLUSH_CONFIRMED`` sentinel.

WAL format (one JSON line per increment, then a sentinel on flush):
  {"k": ["src", "dst", "edge_type"], "d": 1, "cid": "cap-id", "v": 1}
  FLUSH_CONFIRMED

A delta the store refused is written back after the sentinel as one counted
line, ``{"k": [...], "cid": "cap-id", "n": 3, "nv": 2}`` (``n`` calls, ``nv``
verified); a line without ``n`` is one increment.

Recovery rule: replay all lines; after the last ``FLUSH_CONFIRMED`` line
restart accumulation from zero (everything before was already flushed to
KGStore). The sentinel is written only after the store write
(:meth:`CRDTAccumulator.confirm`), so a crash in between replays the
unconfirmed deltas: delivery is at-least-once.
"""
from __future__ import annotations

import json
import logging
import os
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque

from novafabric._safe_path import fsync_dir

logger = logging.getLogger(__name__)


@dataclass
class GCounter:
    """Grow-only counter for a single (src, dst, edge_type) triple.

    Invariant: call_count >= verified_count >= 0
    """

    call_count: int = 0
    verified_count: int = 0
    # Rolling window of representative capsule IDs (most recent first, max 10)
    representative_capsule_ids: Deque[str] = field(
        default_factory=lambda: deque(maxlen=10)
    )

    def increment(self, *, novaseal_valid: bool, capsule_id: str) -> None:
        """Record one observation from *capsule_id*."""
        self.call_count += 1
        if novaseal_valid:
            self.verified_count += 1
            self.representative_capsule_ids.appendleft(capsule_id)
        elif capsule_id and not novaseal_valid:
            # Still record non-verified capsules (lower priority)
            if len(self.representative_capsule_ids) < 10:
                self.representative_capsule_ids.append(capsule_id)

    def absorb(self, *, call_count: int, verified_count: int, capsule_id: str) -> None:
        """Add a whole delta back (a re-queued or replayed counted WAL line)."""
        self.call_count += call_count
        self.verified_count += min(verified_count, call_count)
        if capsule_id and capsule_id not in self.representative_capsule_ids:
            self.representative_capsule_ids.append(capsule_id)

    def merge(self, other: GCounter) -> GCounter:
        """Merge two G-Counters using elementwise max (CRDT join)."""
        merged = GCounter(
            call_count=max(self.call_count, other.call_count),
            verified_count=max(self.verified_count, other.verified_count),
        )
        # Combine representative capsule IDs — keep most recent 10
        seen: set[str] = set()
        for cid in list(self.representative_capsule_ids) + list(
            other.representative_capsule_ids
        ):
            if cid not in seen:
                seen.add(cid)
                merged.representative_capsule_ids.append(cid)
                if len(merged.representative_capsule_ids) >= 10:
                    break
        return merged

    @property
    def confidence(self) -> float:
        """Fraction of calls that carry a valid NovaSeal signature."""
        if self.call_count == 0:
            return 0.0
        return self.verified_count / self.call_count

    @property
    def representative_capsule_id(self) -> str:
        """Most recent representative capsule ID, or empty string."""
        return next(iter(self.representative_capsule_ids), "")

    def to_dict(self) -> dict[str, int | float | str]:
        return {
            "call_count": self.call_count,
            "verified_count": self.verified_count,
            "confidence": self.confidence,
            "representative_capsule_id": self.representative_capsule_id,
        }


class CRDTAccumulator:
    """Accumulates G-Counter increments keyed by (src, dst, edge_type).

    Designed for the hot ingest path: accumulate() is O(1) dict lookup;
    flush() batches all deltas for a single round-trip to KGStore.

    WAL support
    -----------
    When *wal_path* is supplied every increment is appended to a WAL file
    before the in-memory state is updated.  On construction the WAL is
    replayed to recover from a crash: all increments after the last
    ``FLUSH_CONFIRMED`` sentinel are re-applied.

    WAL files grow without bound.  After a successful flush() call the WAL
    is truncated (overwritten with just the sentinel) so it only holds the
    delta for the *next* flush interval.
    """

    WAL_SENTINEL: bytes = b"FLUSH_CONFIRMED\n"

    def __init__(self, wal_path: str | Path | None = None) -> None:
        self._counters: dict[tuple[str, str, str], GCounter] = {}
        self._wal_path: Path | None = Path(wal_path) if wal_path is not None else None
        if self._wal_path is not None:
            self._wal_path.parent.mkdir(parents=True, exist_ok=True)
            self._replay_wal()

    # ------------------------------------------------------------------
    # WAL helpers
    # ------------------------------------------------------------------

    def _replay_wal(self) -> None:
        """Read the WAL file and replay all increments after the last sentinel."""
        assert self._wal_path is not None
        if not self._wal_path.exists():
            return
        try:
            pending: list[dict[str, Any]] = []
            with self._wal_path.open("rb") as fh:
                for raw in fh:
                    line = raw.rstrip(b"\n")
                    if line == b"FLUSH_CONFIRMED":
                        # Everything up to here was already flushed — discard
                        pending = []
                    else:
                        try:
                            entry = json.loads(line)
                            pending.append(entry)
                        except json.JSONDecodeError:
                            logger.warning("WAL: skipping invalid line: %r", line[:80])
            # Replay pending entries without writing back to WAL
            for entry in pending:
                k = tuple(entry["k"])  # (src, dst, edge_type)
                if len(k) != 3:
                    continue
                key = (str(k[0]), str(k[1]), str(k[2]))
                if key not in self._counters:
                    self._counters[key] = GCounter()
                capsule_id = str(entry.get("cid", ""))
                if "n" in entry:  # a re-queued delta: one counted line
                    self._counters[key].absorb(
                        call_count=int(entry["n"]),
                        verified_count=int(entry.get("nv", 0)),
                        capsule_id=capsule_id,
                    )
                else:
                    self._counters[key].increment(
                        novaseal_valid=bool(entry.get("v", 0)), capsule_id=capsule_id
                    )
            if pending:
                logger.info("WAL: replayed %d pending increments after crash", len(pending))
        except Exception as exc:
            logger.error("WAL: replay failed (%s) — starting with empty state", exc)
            self._counters.clear()

    def _wal_append(
        self,
        src: str,
        dst: str,
        edge_type: str,
        capsule_id: str,
        novaseal_valid: bool,
    ) -> None:
        """Append one increment entry to the WAL file (fsync for durability)."""
        assert self._wal_path is not None
        entry = json.dumps(
            {"k": [src, dst, edge_type], "cid": capsule_id, "v": int(novaseal_valid)},
            separators=(",", ":"),
        )
        with self._wal_path.open("ab") as fh:
            fh.write(entry.encode() + b"\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _wal_confirm_flush(self, requeue: list[dict[str, int | float | str]]) -> None:
        """Replace the WAL with the sentinel plus one counted line per *requeue* delta.

        Written to a sibling file and renamed over the WAL, so a crash leaves
        either the old WAL (replayed in full) or the new one — never a WAL
        truncated before the re-queued deltas reached it.
        """
        assert self._wal_path is not None
        body = self.WAL_SENTINEL + b"".join(
            json.dumps(
                {
                    "k": [d["src"], d["dst"], d["edge_type"]],
                    "cid": d.get("representative_capsule_id", ""),
                    "n": int(d["call_count"]),
                    "nv": int(d["verified_count"]),
                },
                separators=(",", ":"),
            ).encode() + b"\n"
            for d in requeue
        )
        tmp = self._wal_path.with_name(self._wal_path.name + ".tmp")
        with tmp.open("wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._wal_path)
        fsync_dir(self._wal_path.parent)  # the rename itself survives a crash

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def accumulate(
        self,
        src: str,
        dst: str,
        edge_type: str,
        capsule_id: str,
        *,
        novaseal_valid: bool = False,
    ) -> None:
        """Record one edge observation."""
        if self._wal_path is not None:
            self._wal_append(src, dst, edge_type, capsule_id, novaseal_valid)
        key = (src, dst, edge_type)
        if key not in self._counters:
            self._counters[key] = GCounter()
        self._counters[key].increment(
            novaseal_valid=novaseal_valid, capsule_id=capsule_id
        )

    def pending(self) -> list[dict[str, int | float | str]]:
        """The accumulated deltas, without resetting anything.

        Each dict has keys: src, dst, edge_type, call_count, verified_count,
        confidence, representative_capsule_id. Write them to the store, then
        call :meth:`confirm` — never the other way round.
        """
        return [
            {"src": src, "dst": dst, "edge_type": edge_type, **counter.to_dict()}
            for (src, dst, edge_type), counter in self._counters.items()
        ]

    def confirm(self, requeue: list[dict[str, int | float | str]] | tuple[()] = ()) -> None:
        """The store has the pending deltas except *requeue*: reset to just those.

        With a WAL, the sentinel and the re-queued deltas replace the log
        atomically, so a delta the store refused survives a restart.
        """
        requeued = list(requeue)
        if self._wal_path is not None:
            self._wal_confirm_flush(requeued)
        self._counters.clear()
        for delta in requeued:
            key = (str(delta["src"]), str(delta["dst"]), str(delta["edge_type"]))
            self._counters.setdefault(key, GCounter()).absorb(
                call_count=int(delta["call_count"]),
                verified_count=int(delta["verified_count"]),
                capsule_id=str(delta.get("representative_capsule_id", "")),
            )

    def flush(self) -> list[dict[str, int | float | str]]:
        """Return all accumulated deltas and reset (:meth:`pending` + :meth:`confirm`).

        For a caller with nowhere to fail; a store writer uses the two halves
        so the WAL is confirmed only after the write.
        """
        deltas = self.pending()
        self.confirm()
        return deltas

    def __len__(self) -> int:
        """Total accumulated observations (sum of call_count across all edges)."""
        return sum(c.call_count for c in self._counters.values())
