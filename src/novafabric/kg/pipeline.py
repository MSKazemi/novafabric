"""OTel span / capsule event → KG entity extraction pipeline.

Five stages:
  1. event     — receive a dict from a capsule events file
  2. normalise — canonicalise all raw entity strings
  3. resolve   — Tier 2/3 alias table lookup + review queue (optional)
  4. accumulate — update CRDT G-Counter in memory (WAL-backed when configured)
  5. aggregate  — flush() to KGStore (one round-trip per batch)

Supported event_type values:
  ModelCallCompleted, ModelCallStarted  → CALLS edge (Agent → Model)
  ToolCallCompleted, ToolCallStarted    → USES_TOOL edge (Agent → Tool)
  EndpointRouted                        → ROUTES_TO edge (Agent → InferenceEndpoint)

Prometheus metrics
------------------
When prometheus-client is installed the following metrics are exported:
  nova_kg_events_total{event_type}    — counter; events processed
  nova_kg_merge_errors_total          — counter; KGStore write failures
  nova_kg_entity_resolution_queue_depth — gauge; pending entities in Tier-3 queue
  nova_kg_crdt_flush_duration_seconds — histogram; CRDT flush + KGStore write duration
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from novafabric.kg.crdt import CRDTAccumulator
from novafabric.kg.entity_normaliser import EntityNormaliser
from novafabric.kg.store import KGStore

if TYPE_CHECKING:
    from novafabric.kg.alias_resolver import AliasEntry
    from novafabric.kg.review_queue import ReviewItem


@runtime_checkable
class _AliasResolverProtocol(Protocol):
    """Structural protocol for AliasTableResolver (avoids circular imports)."""

    def resolve(self, alias: str, entity_type: str) -> str | None: ...
    def register(self, entry: AliasEntry) -> None: ...


@runtime_checkable
class _ReviewQueueProtocol(Protocol):
    """Structural protocol for HumanReviewQueueWriter (avoids circular imports)."""

    def enqueue(self, item: ReviewItem) -> None: ...
    def list_pending(self) -> list[ReviewItem]: ...

_HEURISTIC_CONFIDENCE_THRESHOLD = 0.7

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prometheus metrics — initialised lazily so prometheus-client is optional
# ---------------------------------------------------------------------------

_nova_kg_events_total: Any = None
_nova_kg_merge_errors_total: Any = None
_nova_kg_queue_depth: Any = None
_nova_kg_flush_duration: Any = None
_metrics_ready: bool = False


def _init_metrics() -> None:
    """Lazily initialise Prometheus metrics.  No-op if prometheus-client absent."""
    global _nova_kg_events_total, _nova_kg_merge_errors_total
    global _nova_kg_queue_depth, _nova_kg_flush_duration, _metrics_ready
    if _metrics_ready:
        return
    try:
        from prometheus_client import Counter, Gauge, Histogram  # noqa: PLC0415

        _nova_kg_events_total = Counter(
            "nova_kg_events_total",
            "KG events processed",
            ["event_type"],
        )
        _nova_kg_merge_errors_total = Counter(
            "nova_kg_merge_errors_total",
            "KG merge errors (KGStore write failures)",
        )
        _nova_kg_queue_depth = Gauge(
            "nova_kg_entity_resolution_queue_depth",
            "Number of entities pending in Tier-3 human review queue",
        )
        _nova_kg_flush_duration = Histogram(
            "nova_kg_crdt_flush_duration_seconds",
            "CRDT flush + KGStore write duration",
        )
        _metrics_ready = True
    except ImportError:
        pass  # prometheus-client not installed — metrics silently disabled


def _incr_events(event_type: str) -> None:
    if _nova_kg_events_total is not None:
        _nova_kg_events_total.labels(event_type=event_type).inc()


def _incr_merge_errors() -> None:
    if _nova_kg_merge_errors_total is not None:
        _nova_kg_merge_errors_total.inc()


def _set_queue_depth(depth: int) -> None:
    if _nova_kg_queue_depth is not None and depth >= 0:
        _nova_kg_queue_depth.set(depth)

_MODEL_EVENT_TYPES = frozenset({"ModelCallCompleted", "ModelCallStarted"})
_TOOL_EVENT_TYPES = frozenset({"ToolCallCompleted", "ToolCallStarted"})
_ENDPOINT_EVENT_TYPES = frozenset({"EndpointRouted"})


def _normalise_otel_semconv(event: dict[str, Any]) -> dict[str, Any]:
    """Map an OTel GenAI semconv model-calls.jsonl record to the internal KG event schema.

    nova capture writes model-calls.jsonl using OTel GenAI semconv 1.30 field names
    (``gen_ai.request.model``, ``gen_ai.system``, ``endpoint``, ``parent_span_id``)
    rather than the internal KG event schema (``event_type``, ``agent_id``, ``model_id``).
    This function bridges the gap so existing capsules are usable without migration.

    agent_id proxy: ``parent_span_id`` identifies the trace context that issued the call.
    For single-agent capsules all calls share one parent span ID; for multi-agent captures
    (LangGraph supervisor/workers) each sub-agent has its own parent span.
    """
    out: dict[str, Any] = dict(event)  # shallow copy — do not mutate caller's dict
    out.setdefault("event_type", "ModelCallCompleted")
    # agent_id: prefer explicit field; fall back to trace-scoped proxy
    if not out.get("agent_id"):
        out["agent_id"] = (
            event.get("parent_span_id")
            or event.get("model_call_id")
            or ""
        )
    # model_id: map from OTel semconv request/response model field
    if not out.get("model_id"):
        out["model_id"] = (
            event.get("gen_ai.request.model")
            or event.get("gen_ai.response.model")
            or event.get("model")
            or ""
        )
    return out


class KGIngestionPipeline:
    """Five-stage pipeline: event → normalise → resolve → accumulate → aggregate."""

    def __init__(
        self,
        store: KGStore,
        *,
        wal_path: str | None = None,
        alias_resolver: _AliasResolverProtocol | None = None,
        review_queue: _ReviewQueueProtocol | None = None,
    ) -> None:
        _init_metrics()
        self._store = store
        self._normaliser = EntityNormaliser()
        self._accumulator = CRDTAccumulator(wal_path=wal_path)
        # Tier 2: optional AliasTableResolver
        self._alias_resolver: _AliasResolverProtocol | None = alias_resolver
        # Tier 3: optional HumanReviewQueueWriter
        self._review_queue: _ReviewQueueProtocol | None = review_queue

    # ------------------------------------------------------------------
    # Tier 2+3 resolution helper
    # ------------------------------------------------------------------

    def _resolve_entity(
        self,
        name: str,
        entity_type: str,
        capsule_id: str,
    ) -> str:
        """Apply Tier-2 alias lookup and Tier-3 review-queue routing.

        Returns the canonical name to use (may be the original if unresolved).
        """
        if self._alias_resolver is None:
            return name

        try:
            canonical = self._alias_resolver.resolve(name, entity_type)
        except Exception as exc:  # noqa: BLE001
            logger.debug("AliasTableResolver.resolve error: %s", exc)
            canonical = None

        if canonical is not None:
            return canonical

        # Not found — apply heuristic confidence estimate.
        # The EntityNormaliser already stripped known version suffixes for models,
        # so any name reaching here is unknown.  We assign 0.5 as a neutral
        # heuristic score; callers may override by passing a pre-computed score.
        heuristic_confidence = 0.5

        if self._review_queue is not None:
            if heuristic_confidence >= _HEURISTIC_CONFIDENCE_THRESHOLD:
                # Strong enough heuristic — auto-register
                try:
                    from datetime import datetime, timezone

                    from novafabric.kg.alias_resolver import AliasEntry

                    self._alias_resolver.register(
                        AliasEntry(
                            alias=name,
                            canonical=name,
                            entity_type=entity_type,
                            confidence=heuristic_confidence,
                            source="heuristic",
                            created_at=datetime.now(timezone.utc),
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Auto-register alias error: %s", exc)
            else:
                # Low confidence — enqueue for human review
                try:
                    from novafabric.kg.review_queue import ReviewItem

                    self._review_queue.enqueue(
                        ReviewItem(
                            alias=name,
                            entity_type=entity_type,
                            context={"capsule_id": capsule_id},
                            suggested_canonical=None,
                            confidence=heuristic_confidence,
                        )
                    )
                    # Update Prometheus queue depth gauge
                    try:
                        depth = len(self._review_queue.list_pending())
                        _set_queue_depth(depth)
                    except Exception:  # noqa: BLE001
                        pass
                except Exception as exc:  # noqa: BLE001
                    logger.debug("ReviewQueue.enqueue error: %s", exc)

        return name

    # ------------------------------------------------------------------
    # Stage 1+2+3+4 — ingest one event
    # ------------------------------------------------------------------

    def ingest_event(self, event: dict[str, Any], *, novaseal_valid: bool = False) -> None:
        """Process one capsule event dict and accumulate KG edges.

        Accepts both the internal KG event schema (event_type + agent_id + model_id)
        and the OTel GenAI semconv format produced by nova capture (gen_ai.request.model,
        parent_span_id, endpoint).  Silently skips events with missing required fields.
        """
        # Normalise OTel GenAI semconv format (model-calls.jsonl) to internal schema.
        if not event.get("event_type") and event.get("gen_ai.request.model"):
            event = _normalise_otel_semconv(event)

        capsule_id = str(event.get("capsule_id", ""))
        raw_agent = str(event.get("agent_id", ""))
        if not raw_agent:
            logger.debug("Skipping event with no agent_id: %r", event.get("event_type"))
            return

        agent_id = self._normaliser.normalise("agent", raw_agent)
        agent_id = self._resolve_entity(agent_id, "agent", capsule_id)
        event_type = str(event.get("event_type", ""))

        if event_type in _MODEL_EVENT_TYPES:
            raw_model = str(event.get("model_id", event.get("model", "")))
            if not raw_model:
                logger.debug("Skipping %s with no model_id/model field", event_type)
                return
            model_id = self._normaliser.normalise("model", raw_model)
            model_id = self._resolve_entity(model_id, "model", capsule_id)
            self._accumulator.accumulate(
                agent_id, model_id, "CALLS", capsule_id, novaseal_valid=novaseal_valid
            )
            _incr_events(event_type)

        elif event_type in _TOOL_EVENT_TYPES:
            raw_tool = str(event.get("tool_name", event.get("tool_id", "")))
            if not raw_tool:
                logger.debug("Skipping %s with no tool_name/tool_id field", event_type)
                return
            # MCP server extraction: "filesystem:read_file" → server="filesystem", tool="read_file"
            mcp_server_raw: str | None = None
            if ":" in raw_tool:
                server_part, tool_part = raw_tool.split(":", 1)
                if server_part and tool_part:
                    mcp_server_raw = server_part
                    raw_tool = tool_part
            tool_id = self._normaliser.normalise("tool", raw_tool)
            tool_id = self._resolve_entity(tool_id, "tool", capsule_id)
            self._accumulator.accumulate(
                agent_id, tool_id, "USES_TOOL", capsule_id, novaseal_valid=novaseal_valid
            )
            if mcp_server_raw is not None:
                server_id = self._normaliser.normalise("mcp_server", mcp_server_raw)
                server_id = self._resolve_entity(server_id, "mcp_server", capsule_id)
                self._accumulator.accumulate(
                    tool_id, server_id, "SERVED_BY", capsule_id, novaseal_valid=False
                )
            _incr_events(event_type)

        elif event_type in _ENDPOINT_EVENT_TYPES:
            raw_url = str(event.get("url", event.get("endpoint", "")))
            if not raw_url:
                logger.debug("Skipping %s with no url/endpoint field", event_type)
                return
            endpoint_id = self._normaliser.normalise("endpoint", raw_url)
            endpoint_id = self._resolve_entity(endpoint_id, "endpoint", capsule_id)
            self._accumulator.accumulate(
                agent_id,
                endpoint_id,
                "ROUTES_TO",
                capsule_id,
                novaseal_valid=novaseal_valid,
            )
            _incr_events(event_type)

    # ------------------------------------------------------------------
    # Stage 5 — flush accumulated deltas to KGStore
    # ------------------------------------------------------------------

    def flush_to_store(self) -> int:
        """Flush accumulated deltas to KuzuDB.  Returns number of edges written."""
        t0 = time.monotonic()
        deltas = self._accumulator.flush()
        if not deltas:
            return 0

        written = 0
        for delta in deltas:
            try:
                edge_type = str(delta["edge_type"])
                src = str(delta["src"])
                dst = str(delta["dst"])
                cc = int(delta["call_count"])
                vc = int(delta["verified_count"])
                conf = float(delta["confidence"])
                cid = str(delta["representative_capsule_id"])

                if edge_type == "CALLS":
                    self._store.upsert_calls_edge(
                        agent_id=src,
                        model_id=dst,
                        call_count=cc,
                        verified_count=vc,
                        confidence=conf,
                        capsule_id=cid,
                    )
                elif edge_type == "USES_TOOL":
                    self._store.upsert_uses_tool_edge(
                        agent_id=src,
                        tool_id=dst,
                        call_count=cc,
                        verified_count=vc,
                        confidence=conf,
                        capsule_id=cid,
                    )
                elif edge_type == "ROUTES_TO":
                    self._store.upsert_routes_to_edge(
                        agent_id=src,
                        endpoint_id=dst,
                        call_count=cc,
                        verified_count=vc,
                        confidence=conf,
                        capsule_id=cid,
                    )
                elif edge_type == "SERVED_BY":
                    self._store.upsert_served_by_edge(
                        tool_id=src,
                        server_id=dst,
                        call_count=cc,
                        verified_count=vc,
                        confidence=conf,
                        capsule_id=cid,
                    )
                else:
                    logger.warning("Unknown edge_type %r — skipping delta", edge_type)
                    continue

                written += 1
            except Exception as exc:
                _incr_merge_errors()
                logger.warning(
                    "Failed to write KG edge %r → %r: %s",
                    delta.get("src"),
                    delta.get("dst"),
                    exc,
                )

        elapsed = time.monotonic() - t0
        if _nova_kg_flush_duration is not None:
            _nova_kg_flush_duration.observe(elapsed)

        return written

    # ------------------------------------------------------------------
    # Convenience property
    # ------------------------------------------------------------------

    @property
    def pending_count(self) -> int:
        """Number of pending (not yet flushed) observations."""
        return len(self._accumulator)
