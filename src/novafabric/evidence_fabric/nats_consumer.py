"""NATS JetStream pull consumer for Evidence Fabric scale tier.

Provides the same interface as :class:`EventQueueConsumer` but backed by a
real NATS JetStream subject.  Events are pulled in batches and routed to a
:class:`DuckDBAccumulator` (local mode) or a compatible accumulator that
implements ``ingest_edges`` / ``ingest_capsule_events``.

cap-006 / ADR-0066 (Evidence Fabric v1.0 — scale tier)

Environment variables
---------------------
``NOVA_NATS_URL``
    NATS server URL (default: ``nats://localhost:4222``).
``NOVA_NATS_STREAM``
    JetStream stream name (default: ``nova-evidence``).
``NOVA_NATS_SUBJECT``
    Subject filter for the consumer (default: ``nova.evidence.>``).
``NOVA_NATS_CONSUMER``
    Durable consumer name (default: ``nova-evidence-consumer``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_DEAD_LETTER_MAX = 1_000

# -------------------------------------------------------------------
# Optional import gate
# -------------------------------------------------------------------
try:
    import nats
    from nats.js.api import ConsumerConfig, DeliverPolicy

    _NATS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _NATS_AVAILABLE = False


def _require_nats() -> None:
    if not _NATS_AVAILABLE:
        raise ImportError(
            "nats-py is required for the NATS JetStream consumer. "
            "Install it with: pip install novafabric[nats]"
        )


class NATSJetStreamConsumer:
    """NATS JetStream pull consumer — same interface as EventQueueConsumer.

    Pulls events from a JetStream subject, batches them, and routes them to an
    accumulator.  Failed events are placed on a bounded dead-letter list instead
    of being silently dropped.

    Args:
        accumulator:  Any object with ``ingest_edges(list[dict])`` and
                      ``ingest_capsule_events(list[dict])`` methods.  Defaults
                      to ``DuckDBAccumulator(Path(":memory:"))`` if not supplied.
        batch_size:   Maximum events pulled per NATS ``fetch`` call (default 100).
        fetch_timeout: Seconds to wait for a JetStream ``fetch`` batch (default 1.0).
    """

    def __init__(
        self,
        accumulator: Any = None,
        batch_size: int = 100,
        fetch_timeout: float = 1.0,
    ) -> None:
        _require_nats()

        if accumulator is None:
            from pathlib import Path

            from novafabric.evidence_fabric.duckdb_accumulator import (
                DuckDBAccumulator,
            )

            accumulator = DuckDBAccumulator(Path(":memory:"))

        self._accumulator = accumulator
        self._batch_size = batch_size
        self._fetch_timeout = fetch_timeout

        # Config from env
        self._nats_url = os.environ.get("NOVA_NATS_URL", "nats://localhost:4222")
        self._stream_name = os.environ.get("NOVA_NATS_STREAM", "nova-evidence")
        self._subject = os.environ.get("NOVA_NATS_SUBJECT", "nova.evidence.>")
        self._consumer_name = os.environ.get(
            "NOVA_NATS_CONSUMER", "nova-evidence-consumer"
        )

        # Internal state (Any to avoid NameError when nats-py is absent at parse time)
        self._nc: Any = None
        self._js: Any = None
        self._sub: Any = None
        self._dead_letter: list[dict[str, Any]] = []
        self._processed = 0
        self._errors = 0
        self._running = False
        self._task: asyncio.Task[None] | None = None

        # Injection queue — used by put() for tests only
        self._inject_queue: asyncio.Queue[bytes] = asyncio.Queue()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect to NATS and start the background pull loop."""
        if self._running:
            return
        self._nc = await nats.connect(self._nats_url)
        self._js = self._nc.jetstream()
        try:
            await self._js.subscribe(
                self._subject,
                stream=self._stream_name,
                durable=self._consumer_name,
                config=ConsumerConfig(deliver_policy=DeliverPolicy.NEW),
            )
        except Exception:  # noqa: BLE001
            # Consumer/stream may already exist or be pre-configured on the server.
            pass

        self._sub = await self._js.pull_subscribe(
            self._subject,
            durable=self._consumer_name,
            stream=self._stream_name,
        )
        self._running = True
        self._task = asyncio.create_task(
            self._drain_loop(), name="nova-nats-jetstream-consumer"
        )
        logger.debug(
            "NATSJetStreamConsumer: started (url=%s, stream=%s)",
            self._nats_url,
            self._stream_name,
        )

    async def stop(self) -> None:
        """Stop pulling, drain in-flight messages, close NATS connection."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        # Drain the injection queue
        while not self._inject_queue.empty():
            try:
                raw = self._inject_queue.get_nowait()
                await self._process_raw([raw])
            except asyncio.QueueEmpty:
                break

        if self._nc is not None:
            try:
                await self._nc.drain()
            except Exception:  # noqa: BLE001
                pass
            self._nc = None
            self._js = None
            self._sub = None

        logger.debug(
            "NATSJetStreamConsumer: stopped (processed=%d, errors=%d)",
            self._processed,
            self._errors,
        )

    # ------------------------------------------------------------------
    # Test-injection API
    # ------------------------------------------------------------------

    async def put(self, event_json: bytes) -> None:
        """Inject a raw JSON bytes event for testing (bypasses NATS).

        In production use, events arrive via JetStream; this method is
        provided so tests can inject events without a live NATS server.
        """
        await self._inject_queue.put(event_json)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def dead_letter_count(self) -> int:
        """Number of events in the dead-letter list."""
        return len(self._dead_letter)

    @property
    def stats(self) -> dict[str, int]:
        """Return consumer statistics."""
        return {
            "processed": self._processed,
            "errors": self._errors,
            "dead_letter": self.dead_letter_count,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _drain_loop(self) -> None:
        """Background task: pull from JetStream and process injection queue."""
        while self._running:
            # Process injected test events first
            injected: list[bytes] = []
            while not self._inject_queue.empty():
                try:
                    injected.append(self._inject_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if injected:
                await self._process_raw(injected)

            # Pull from NATS when connected
            if self._sub is not None:
                try:
                    msgs = await self._sub.fetch(
                        self._batch_size, timeout=self._fetch_timeout
                    )
                    raw_batch = [msg.data for msg in msgs]
                    for msg in msgs:
                        try:
                            await msg.ack()
                        except Exception:  # noqa: BLE001
                            pass
                    if raw_batch:
                        await self._process_raw(raw_batch)
                    else:
                        # A fetch that returns no messages without blocking (e.g. an
                        # empty stream, or a mocked client) must still yield control,
                        # or this loop starves the event loop and stop() can never run.
                        await asyncio.sleep(0)
                except Exception:  # noqa: BLE001 — timeout or fetch error
                    await asyncio.sleep(0.05)
            else:
                # No NATS connection — idle wait (test mode or not yet connected)
                await asyncio.sleep(0.05)

    async def _process_raw(self, raw_messages: list[bytes]) -> None:
        """Decode JSON, route, and ingest a batch of raw event bytes."""
        edges: list[dict[str, Any]] = []
        capsule_events: list[dict[str, Any]] = []
        failed: list[bytes] = []

        for raw in raw_messages:
            try:
                event: dict[str, Any] = json.loads(raw)
            except (ValueError, UnicodeDecodeError) as exc:
                logger.warning("NATSJetStreamConsumer: failed to decode event: %s", exc)
                failed.append(raw)
                continue
            if event.get("event_type") == "lineage_edge":
                edges.append(event)
            else:
                capsule_events.append(event)

        loop = asyncio.get_event_loop()

        if edges:
            try:
                await loop.run_in_executor(
                    None, self._accumulator.ingest_edges, edges
                )
                self._processed += len(edges)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "NATSJetStreamConsumer: failed to ingest %d edges: %s",
                    len(edges),
                    exc,
                )
                self._errors += len(edges)
                self._add_to_dead_letter(
                    [{"_raw": m.decode(errors="replace"), "_error": str(exc)} for m in
                     [json.dumps(e).encode() for e in edges]]
                )

        if capsule_events:
            try:
                await loop.run_in_executor(
                    None, self._accumulator.ingest_capsule_events, capsule_events
                )
                self._processed += len(capsule_events)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "NATSJetStreamConsumer: failed to ingest %d capsule events: %s",
                    len(capsule_events),
                    exc,
                )
                self._errors += len(capsule_events)
                self._add_to_dead_letter(
                    [{"_raw": json.dumps(e), "_error": str(exc)} for e in capsule_events]
                )

        for raw in failed:
            self._add_to_dead_letter(
                [{"_raw": raw.decode(errors="replace"), "_error": "json_decode_error"}]
            )

    def _add_to_dead_letter(self, events: list[dict[str, Any]]) -> None:
        """Add failed event records to the dead-letter list (bounded)."""
        remaining = _DEAD_LETTER_MAX - len(self._dead_letter)
        if remaining <= 0:
            logger.warning(
                "NATSJetStreamConsumer: dead-letter list full (%d); "
                "discarding %d events",
                _DEAD_LETTER_MAX,
                len(events),
            )
            return
        take = min(len(events), remaining)
        self._dead_letter.extend(events[:take])
        if take < len(events):
            logger.warning(
                "NATSJetStreamConsumer: dead-letter overflow, discarded %d events",
                len(events) - take,
            )
