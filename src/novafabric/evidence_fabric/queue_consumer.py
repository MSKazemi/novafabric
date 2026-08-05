"""In-process asyncio queue consumer for Evidence Fabric self-contained mode.

Provides the same pull-consumer interface as a NATS JetStream consumer but
backed by an in-process ``asyncio.Queue``.  Events are batched and written
to a ``DuckDBAccumulator``.  Failed events go to a bounded dead-letter list
rather than being silently dropped.

cap-002 / ADR-0066 (Evidence Fabric v1.0)
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing-only, never executed at runtime
    # Annotation-only use (the module has `from __future__ import annotations`,
    # so this never needs to resolve at runtime). Kept out of the runtime import
    # graph by ADR-0222 so EventQueueConsumer — which touches duckdb only through
    # the duck-typed accumulator handed to it — stays importable on a plain
    # `pip install novafabric`, without the `scale` extra.
    from novafabric.evidence_fabric.duckdb_accumulator import DuckDBAccumulator

logger = logging.getLogger(__name__)

_QUEUE_MAX = 10_000
_DEAD_LETTER_MAX = 1_000


class EventQueueConsumer:
    """asyncio.Queue consumer — same interface as NATS JetStream pull consumer.

    Consumed events are routed by ``event_type``:
    - ``"lineage_edge"``   → ``DuckDBAccumulator.ingest_edges``
    - ``"model_call"`` / ``"tool_call"`` / anything else
                          → ``DuckDBAccumulator.ingest_capsule_events``

    Args:
        accumulator: A :class:`DuckDBAccumulator` instance to write into.
        batch_size:  Maximum number of events to drain per ``_process_batch``
                     call (default 100).
    """

    def __init__(
        self, accumulator: DuckDBAccumulator, batch_size: int = 100
    ) -> None:
        self._accumulator = accumulator
        self._batch_size = batch_size
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=_QUEUE_MAX
        )
        self._dead_letter: list[dict[str, Any]] = []
        self._processed = 0
        self._errors = 0
        self._running = False
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background drain loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(
            self._drain_loop(), name="nova-evidence-queue-consumer"
        )
        logger.debug("EventQueueConsumer: started (batch_size=%d)", self._batch_size)

    async def stop(self) -> None:
        """Stop the background drain loop and flush remaining events."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # Flush any remaining events synchronously.
        while not self._queue.empty():
            await self._process_batch()
        logger.debug(
            "EventQueueConsumer: stopped (processed=%d, errors=%d)",
            self._processed,
            self._errors,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def publish(self, event: dict[str, Any]) -> None:
        """Enqueue a single event for processing.

        Blocks if the queue is full (back-pressure).  Never drops silently.
        If the queue is full the call will wait until space is available.
        """
        await self._queue.put(event)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _drain_loop(self) -> None:
        """Background task: drain the queue in batches."""
        while self._running:
            if not self._queue.empty():
                await self._process_batch()
            else:
                await asyncio.sleep(0.05)

    async def _process_batch(self) -> int:
        """Drain up to ``batch_size`` events from the queue and write them.

        Returns:
            Number of events processed in this batch.
        """
        edges: list[dict[str, Any]] = []
        capsule_events: list[dict[str, Any]] = []
        count = 0

        while count < self._batch_size and not self._queue.empty():
            try:
                event = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            count += 1
            if event.get("event_type") == "lineage_edge":
                edges.append(event)
            else:
                capsule_events.append(event)

        if not edges and not capsule_events:
            return 0

        loop = asyncio.get_event_loop()

        if edges:
            try:
                await loop.run_in_executor(
                    None, self._accumulator.ingest_edges, edges
                )
                self._processed += len(edges)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "EventQueueConsumer: failed to ingest %d edges: %s",
                    len(edges),
                    exc,
                )
                self._errors += len(edges)
                self._add_to_dead_letter(edges)

        if capsule_events:
            try:
                await loop.run_in_executor(
                    None, self._accumulator.ingest_capsule_events, capsule_events
                )
                self._processed += len(capsule_events)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "EventQueueConsumer: failed to ingest %d capsule events: %s",
                    len(capsule_events),
                    exc,
                )
                self._errors += len(capsule_events)
                self._add_to_dead_letter(capsule_events)

        return count

    def _add_to_dead_letter(self, events: list[dict[str, Any]]) -> None:
        """Add failed events to the dead-letter list, respecting the max cap."""
        remaining_capacity = _DEAD_LETTER_MAX - len(self._dead_letter)
        if remaining_capacity <= 0:
            logger.warning(
                "EventQueueConsumer: dead-letter list full (%d); "
                "discarding %d events",
                _DEAD_LETTER_MAX,
                len(events),
            )
            return
        take = min(len(events), remaining_capacity)
        self._dead_letter.extend(events[:take])
        if take < len(events):
            logger.warning(
                "EventQueueConsumer: dead-letter list nearly full; "
                "discarded %d overflow events",
                len(events) - take,
            )

    @property
    def stats(self) -> dict[str, int]:
        """Return consumer statistics.

        Returns:
            ``{"queued": N, "processed": N, "errors": N, "dead_letter": N}``
        """
        return {
            "queued": self._queue.qsize(),
            "processed": self._processed,
            "errors": self._errors,
            "dead_letter": len(self._dead_letter),
        }

    @property
    def dead_letter(self) -> list[dict[str, Any]]:
        """Read-only view of the dead-letter list."""
        return list(self._dead_letter)
