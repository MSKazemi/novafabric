"""DeltaBuffer — 60s in-memory ring buffer for TDP events.

Assigns monotonic checkpoint IDs to every enqueued event.
Supports replay within the 60-second window.
Supports live pub-sub callbacks for real-time push to WS clients.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class _StoredEvent:
    seq: int
    event: dict[str, Any]
    enqueued_at: float  # time.monotonic()


class DeltaBuffer:
    """Thread-safe ring buffer for TDP delta events.

    Bounded to 60 000 events (≈ 1 000 events/s × 60 s).
    Checkpoint IDs are monotonic integers starting at 1.

    Live pub-sub: callers can register callbacks via ``subscribe()``.
    Each call to ``enqueue()`` notifies all registered callbacks under
    the buffer lock so no events are missed between subscribe and replay.
    """

    _MAX_EVENTS = 60_000
    _WINDOW_SECONDS = 60.0

    def __init__(self) -> None:
        self._deque: deque[_StoredEvent] = deque()
        self._seq = 0
        self._lock = threading.Lock()
        self._callbacks: dict[str, Callable[[dict[str, Any]], None]] = {}

    # ------------------------------------------------------------------
    # Pub-sub
    # ------------------------------------------------------------------

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> str:
        """Register *callback* to be invoked on every enqueued event.

        Returns a subscriber_id that can be passed to ``unsubscribe()``.
        The callback is invoked synchronously under the buffer lock, so it
        must be non-blocking.  Async WS handlers should pass a callback that
        calls ``loop.call_soon_threadsafe(queue.put_nowait, event)``.
        """
        sub_id = str(uuid.uuid4())
        with self._lock:
            self._callbacks[sub_id] = callback
        return sub_id

    def unsubscribe(self, subscriber_id: str) -> None:
        """Remove the callback registered under *subscriber_id* (no-op if absent)."""
        with self._lock:
            self._callbacks.pop(subscriber_id, None)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def enqueue(self, event: dict[str, Any]) -> int:
        """Assign a monotonic seq to *event* and store it. Returns seq.

        Immediately notifies all registered pub-sub callbacks with the
        stamped event so live WS clients receive events with sub-ms latency
        instead of waiting for the next heartbeat cycle.
        """
        with self._lock:
            self._seq += 1
            stamped: dict[str, Any] = {**event, "seq": self._seq}
            stored = _StoredEvent(
                seq=self._seq, event=stamped, enqueued_at=time.monotonic()
            )
            self._deque.append(stored)
            if len(self._deque) > self._MAX_EVENTS:
                self._deque.popleft()
            seq = self._seq
            # Notify subscribers while still holding the lock so that a
            # subscribe() + replay_since() pair never races with enqueue().
            for cb in list(self._callbacks.values()):
                try:
                    cb(stamped)
                except Exception:
                    pass
        return seq

    def checkpoint(self) -> dict[str, Any]:
        """Emit a batch_checkpoint event and store it. Returns the event."""
        import time as _time
        event: dict[str, Any] = {
            "type": "batch_checkpoint",
            "ts_ms": int(_time.time() * 1000),
        }
        seq = self.enqueue(event)
        return {**event, "seq": seq}

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def replay_since(self, checkpoint_id: int) -> list[dict[str, Any]]:
        """Return all events with seq > checkpoint_id within the 60-second window.

        Returns an empty list if checkpoint_id is outside the retention window
        (i.e., it is older than _WINDOW_SECONDS).
        """
        now = time.monotonic()
        cutoff = now - self._WINDOW_SECONDS

        with self._lock:
            # Find the oldest stored event; if the checkpoint is before the window start,
            # return empty (gap too large — client must do a full reset).
            if self._deque and checkpoint_id > 0:
                oldest = self._deque[0]
                if oldest.enqueued_at < cutoff and oldest.seq > checkpoint_id:
                    return []

            return [
                se.event
                for se in self._deque
                if se.seq > checkpoint_id and se.enqueued_at >= cutoff
            ]

    @property
    def latest_seq(self) -> int:
        with self._lock:
            return self._seq
