"""NATS JetStream LineageConsumer — requires nats-py (optional extra).

cap-006 / ADR-0066.

Modes:
- In-process (no NATS): call ``run_once(events)`` directly.
- NATS JetStream pull consumer: call ``run_from_nats()``.
  Requires NOVA_NATS_URL env var and ``nats-py`` package (``novafabric[scale]``).

Bulk insertion to KuzuDB uses a DuckDB Arrow accumulator → Parquet → KuzuDB
COPY path for high-throughput edge ingestion.  Requires ``pyarrow`` and a
``kuzu`` connection to be provided when using ``bulk_insert_edges``.

SCALE-ADR-001 dedup: at-least-once JetStream delivery is compensated for on
two levels — server-side, the stream this consumer creates carries an
explicit, operator-configurable ``Nats-Msg-Id`` duplicate window
(``duplicate_window_s`` / ``NOVA_NATS_DUPLICATE_WINDOW_S``, default 120s);
consumer-side, ``run_once()`` dedups by ``event_id`` (ULID) against a bounded
cache that persists across fetch() batches for the consumer's lifetime, not
just within one batch. Whether the *default* window is sufficient for real
HPC reconnect latency remains an open, genuinely hardware-gated benchmark —
this module only makes the window and the dedup cache size configurable.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import OrderedDict
from datetime import timedelta
from typing import Any, Callable

logger = logging.getLogger(__name__)

# SCALE-ADR-001: consumer-side ULID dedup compensates for NATS JetStream's
# at-least-once delivery (a message can be redelivered across separate
# fetch() batches, e.g. after an ack is lost or a nak-timeout). Bounded FIFO
# eviction (not an unbounded set) per CLAUDE.md "bounded queues, bounded
# retries" — a long-running consumer must not leak memory over its lifetime.
DEFAULT_DEDUP_CACHE_SIZE = 50_000


class LineageConsumer:
    """Pulls capsule events from NATS JetStream and derives lineage edges into KuzuDB.

    Requires: NOVA_NATS_URL env var and nats-py package (novafabric[scale]).
    """

    def __init__(
        self,
        nats_url: str | None = None,
        kuzu_path: str | None = None,
        dedup_cache_size: int = DEFAULT_DEDUP_CACHE_SIZE,
        duplicate_window_s: float | None = None,
    ) -> None:
        self.nats_url = nats_url or os.getenv(
            "NOVA_NATS_URL", "nats://localhost:4222"
        )
        self.kuzu_path = kuzu_path or os.getenv(
            "NOVA_KUZU_PATH", ".nova/kg/lineage.kuzu"
        )
        self._edge_handlers: dict[str, Callable[..., Any]] = {}
        # Persists across run_once() calls (i.e. across NATS fetch batches)
        # for the consumer's lifetime — see SCALE-ADR-001 dedup note above.
        self._dedup_cache_size = dedup_cache_size
        self._seen_event_ids: OrderedDict[str, None] = OrderedDict()
        # Server-side JetStream Nats-Msg-Id dedup window for the stream this
        # consumer creates (SCALE-ADR-001). NATS' own default is 2 minutes;
        # exposed here so an operator can widen it once real HPC reconnect
        # latency is benchmarked — that benchmark itself remains open pending
        # real cluster hardware, this only makes the window configurable.
        self.duplicate_window_s = duplicate_window_s or float(
            os.getenv("NOVA_NATS_DUPLICATE_WINDOW_S", "120")
        )
        logger.info(
            "LineageConsumer configured: nats=%s kuzu=%s dedup_cache_size=%d "
            "duplicate_window_s=%.0f",
            self.nats_url,
            self.kuzu_path,
            self._dedup_cache_size,
            self.duplicate_window_s,
        )

    async def _process_event(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract lineage edges from a single capsule event."""
        edges: list[dict[str, Any]] = []
        event_type = event.get("event_type", "")

        if event_type == "RunStarted" and event.get("parent_run_id"):
            edges.append(
                {
                    "src": event["parent_run_id"],
                    "dst": event["run_id"],
                    "edge_type": "SPAWNED_BY",
                    "source_event_id": event.get("event_id"),
                }
            )
        elif event_type == "ArtifactProduced":
            edges.append(
                {
                    "src": event["run_id"],
                    "dst": event.get("artifact_id", ""),
                    "edge_type": "PRODUCED",
                    "source_event_id": event.get("event_id"),
                }
            )
        elif event_type == "ArtifactConsumed":
            edges.append(
                {
                    "src": event.get("artifact_id", ""),
                    "dst": event["run_id"],
                    "edge_type": "CONSUMED_BY",
                    "source_event_id": event.get("event_id"),
                }
            )

        return edges

    async def run_once(
        self, events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Process a batch of events; return extracted edges.

        Deduplicates by event_id (ULID) against a bounded cache that persists
        across calls for this consumer's lifetime — not just within one
        batch — so a message redelivered in a *later* NATS fetch() (JetStream
        at-least-once semantics) is still caught (SCALE-ADR-001). Safe to
        call without NATS (for testing).
        """
        all_edges: list[dict[str, Any]] = []

        for event in events:
            event_id: str | None = event.get("event_id")
            if event_id is not None:
                if event_id in self._seen_event_ids:
                    logger.debug("Deduplicating event_id=%s (redelivered)", event_id)
                    continue
                self._seen_event_ids[event_id] = None
                if len(self._seen_event_ids) > self._dedup_cache_size:
                    self._seen_event_ids.popitem(last=False)

            edges = await self._process_event(event)
            all_edges.extend(edges)

        return all_edges

    async def run_from_nats(
        self,
        subject: str = "novafabric.lineage.>",
        batch_size: int = 500,
        fetch_timeout: float = 1.0,
    ) -> None:
        """Pull lineage events from NATS JetStream in a continuous loop.

        Requires NOVA_NATS_URL env var to be set and nats-py installed
        (``pip install novafabric[scale]``).

        The consumer creates the ``novafabric-lineage`` stream if it does not
        already exist and uses a durable ``novafabric-lineage-consumer``
        consumer so restarts do not re-process acknowledged events.

        Args:
            subject:       NATS subject pattern to subscribe to.
            batch_size:    Maximum messages to pull per fetch call.
            fetch_timeout: Seconds to wait for each fetch before retrying.
        """
        nats_url = self.nats_url
        if not nats_url or nats_url == "nats://localhost:4222":
            env_url = os.getenv("NOVA_NATS_URL")
            if not env_url:
                raise RuntimeError(
                    "NOVA_NATS_URL not set; LineageConsumer.run_from_nats() requires NATS. "
                    "Install nats-py with: pip install novafabric[scale]"
                )
            nats_url = env_url

        try:
            import nats
            from nats.errors import TimeoutError as NatsTimeoutError
        except ImportError as exc:
            raise RuntimeError(
                "nats-py not installed; install with: pip install novafabric[scale]"
            ) from exc

        logger.info("Connecting to NATS at %s", nats_url)
        nc = await nats.connect(nats_url)
        js = nc.jetstream()

        # Ensure stream exists for the subject.
        stream_name = "novafabric-lineage"
        try:
            await js.find_stream_name_by_subject(subject)
            logger.info("NATS stream '%s' already exists", stream_name)
        except Exception:
            logger.info(
                "Creating NATS stream '%s' for subject '%s' (duplicate_window=%.0fs)",
                stream_name,
                subject,
                self.duplicate_window_s,
            )
            await js.add_stream(
                name=stream_name,
                subjects=[subject],
                duplicate_window=timedelta(seconds=self.duplicate_window_s),
            )

        sub = await js.pull_subscribe(subject, "novafabric-lineage-consumer")
        logger.info(
            "NATS JetStream pull consumer started: subject=%s batch=%d",
            subject,
            batch_size,
        )

        while True:
            try:
                msgs = await sub.fetch(batch=batch_size, timeout=fetch_timeout)
            except NatsTimeoutError:
                # No messages available — normal idle condition.
                continue
            except Exception as exc:
                logger.warning("NATS fetch error (will retry): %s", exc)
                continue

            events: list[dict[str, Any]] = []
            raw_msgs = []
            for msg in msgs:
                try:
                    event = json.loads(msg.data)
                    events.append(event)
                    raw_msgs.append(msg)
                except (json.JSONDecodeError, ValueError) as exc:
                    logger.warning("Skipping malformed NATS message: %s", exc)
                    await msg.ack()

            if events:
                edges = await self.run_once(events)
                logger.debug(
                    "NATS batch: %d events → %d edges", len(events), len(edges)
                )

            for msg in raw_msgs:
                await msg.ack()

    def bulk_insert_edges(
        self,
        edges: list[dict[str, Any]],
        kuzu_conn: Any,
    ) -> int:
        """Bulk-insert lineage edges into KuzuDB via DuckDB Arrow → Parquet → COPY.

        Uses DuckDB as an in-process Arrow accumulator, writes a temp Parquet
        file, then issues a KuzuDB COPY statement for efficient bulk ingestion.

        Args:
            edges:     List of edge dicts with keys: src, dst, edge_type,
                       source_event_id (optional).
            kuzu_conn: An open ``kuzu.Connection`` instance.

        Returns:
            Number of edges inserted (0 if edges list is empty).

        Raises:
            ImportError: if pyarrow is not installed.
        """
        if not edges:
            return 0

        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise ImportError(
                "pyarrow required for bulk_insert_edges; "
                "install with: pip install novafabric[scale]"
            ) from exc

        table = pa.table(
            {
                "src_run_id": pa.array([e["src"] for e in edges], type=pa.string()),
                "dst_run_id": pa.array([e["dst"] for e in edges], type=pa.string()),
                "edge_type": pa.array([e["edge_type"] for e in edges], type=pa.string()),
                "event_id": pa.array(
                    [e.get("source_event_id") or "" for e in edges], type=pa.string()
                ),
            }
        )

        parquet_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".parquet", delete=False, prefix="nova_lineage_"
            ) as f:
                parquet_path = f.name

            pq.write_table(table, parquet_path)  # type: ignore[no-untyped-call]
            logger.debug(
                "bulk_insert_edges: wrote %d edges to %s", len(edges), parquet_path
            )

            kuzu_conn.execute(
                f"COPY LineageEdge FROM '{parquet_path}' (header=true, IGNORE_ERRORS=true)"
            )
            logger.info("bulk_insert_edges: copied %d edges into KuzuDB", len(edges))
            return len(edges)

        except Exception as exc:
            logger.error("bulk_insert_edges failed: %s", exc)
            raise
        finally:
            if parquet_path is not None:
                try:
                    os.unlink(parquet_path)
                except OSError:
                    pass
