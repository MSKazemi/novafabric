# NovaFabric Event Envelope v1 — Normative Specification

**Version:** 1.0.0
**Status:** stable
**Schema pin:** see `envelope-v1.sha256` in this directory (SHA-256 of `envelope-v1.json`)

---

## 1. Overview

Event Envelope v1 is the canonical wire format for all evidence events in NovaFabric. Every event emitted by an AI agent, captured by a collector, or stored in the evidence fabric is encoded as an `EventEnvelope`. The format is designed to be forward-compatible: unknown fields are tolerated (the JSON Schema sets `additionalProperties: true`), and all downstream consumers are required to ignore fields they do not recognise.

The envelope carries both the identity context of the event (who emitted it, in which run, on which cluster) and the observability context (W3C Trace Context IDs) needed to correlate it with OTel spans and logs. Payload content is event-type-specific and intentionally left open: the envelope is a routing and integrity layer, not a data-modelling layer. Downstream case studies — parent/child capsule, object capsule store, metadata database, lineage-at-scale — each extract only the fields they need and ignore the rest.

---

## 2. Field Definitions

All fields listed are top-level properties of the `EventEnvelope` JSON object. Required fields are marked in the **Req?** column.

| Field | Type | Req? | Description |
|---|---|---|---|
| `envelope_version` | `string` (const `"1"`) | Yes | Always the string `"1"`. String type allows forward parsers to distinguish unknown future versions without numeric comparison. |
| `schema_version` | `string` (const `"event-envelope/1"`) | Yes | Compound schema identifier. Must be `"event-envelope/1"` for this version. |
| `event_id` | ULID string | Yes | Globally unique ULID for this event. Used as the content-addressable storage key in the object capsule store. |
| `global_run_id` | ULID or UUID v7 string | Yes | Logical distributed run identifier. Identical across all events in the same distributed run, regardless of how many worker processes are involved. |
| `run_id` | ULID string | Yes | Per-process capsule identifier. Equals `global_run_id` for PARENT and STANDALONE capsules; differs for WORKER and COORDINATOR capsules. |
| `parent_run_id` | ULID string or `null` | Yes | `null` for STANDALONE and PARENT capsules. Non-null ULID pointing to the parent's `run_id` for WORKER and COORDINATOR capsules. |
| `event_type` | `string` enum | Yes | Event kind. Real producers emit one of `RunStarted`, `RunCompleted`, `RunFailed`, `RunAborted` (matching `schemas/event_schema.py::CapsuleEventType` 1:1 — ADR-0220). The original `run.start`, `run.end`, `model_call`, `tool_call`, `span`, `capsule.finalize` values remain schema-valid for backward compatibility but were never actually emitted by any producer in this repository. Indexed by the metadata database. |
| `trace_id` | 32 lowercase hex chars | Yes | W3C Trace Context 16-byte trace ID. Must not be all-zeros. |
| `span_id` | 16 lowercase hex chars | Yes | W3C Trace Context 8-byte span ID. Must not be all-zeros. |
| `agent_id` | non-empty string | Yes | Stable identifier for the emitting agent or SDK component. No whitespace or control characters. |
| `cluster_id` | string or `null` | Yes | Cluster identifier. `null` in local/single-machine mode. |
| `tenant_id` | string or `null` | Yes | Multi-tenancy identifier. `null` in single-tenant/local mode. |
| `started_at` | ISO 8601 date-time string | Yes | Event start time with timezone offset, millisecond precision. Best-effort; do not use for strict ordering — use `event_id` (ULID) for monotonic ordering within a node. |
| `emitter_node_id` | string or `null` | No | Hostname or node identifier of the emitting process. `null` when unavailable (e.g., local mode). |
| `payload` | object or `null` | No | Event-type-specific payload. Schema is defined per `event_type`, not in this envelope schema. Consumers must tolerate unknown keys. |
| `payload_hash` | `"sha256:<64 hex chars>"` or `null` | No | SHA-256 of the canonical serialized payload bytes, in `sha256:<hex>` form. Attached by the collector before forwarding; `null` if not yet hashed. |
| `nova.batch.signature` | 88-char base64 string or `null` | No | Base64-encoded Ed25519 detached signature over the batch, attached by the NovaSeal Batch Processor. `null` before signing. |
| `nova.batch.signing_key_id` | UUID string or `null` | No | UUID identifying the NovaSeal signing key used to produce `nova.batch.signature`. `null` before signing. |

---

## 3. OTLP LogRecord Mapping

When the collector emits events over OTLP, each `EventEnvelope` is serialised as an OTLP `LogRecord`. The mapping below defines the authoritative field-to-attribute correspondence. All `nova.*` attributes are carried in `LogRecord.attributes` unless noted.

| NovaFabric field | OTLP attribute / field | Notes |
|---|---|---|
| `event_id` | `nova.event_id` | Attribute; also used as `LogRecord.observed_time_unix_nano` key for deduplication. |
| `global_run_id` | `nova.global_run_id` | Attribute. |
| `run_id` | `nova.run_id` | Attribute. |
| `parent_run_id` | `nova.parent_run_id` | Attribute; omitted (not set) when `null`. |
| `event_type` | `nova.event_type` | Attribute. |
| `agent_id` | `nova.agent_id` | Attribute. |
| `cluster_id` | `nova.cluster_id` | Attribute; omitted when `null`. |
| `tenant_id` | `nova.tenant_id` | Attribute; omitted when `null`. |
| `envelope_version` | `nova.envelope_version` | Attribute. |
| `schema_version` | `nova.schema_version` | Attribute. |
| `payload_hash` | `nova.payload_hash` | Attribute; omitted when `null`. |
| `emitter_node_id` | `nova.emitter_node_id` | Attribute; omitted when `null`. |
| `trace_id` | `LogRecord.trace_id` | OTLP native 16-byte TraceId field (not an attribute). |
| `span_id` | `LogRecord.span_id` | OTLP native 8-byte SpanId field (not an attribute). |
| `nova.batch.signature` | `Resource.attributes["nova.batch.signature"]` | Resource-level attribute, set per batch by NovaSeal. |
| `nova.batch.signing_key_id` | `Resource.attributes["nova.batch.signing_key_id"]` | Resource-level attribute, set per batch by NovaSeal. |
| `payload` (model_call: model) | `gen_ai.request.model` | OTel GenAI semconv; set from `payload.model` for `model_call` events. |
| `payload` (model_call: input_tokens) | `gen_ai.usage.input_tokens` | OTel GenAI semconv; set from `payload.usage.input_tokens` for `model_call` events. |
| `payload` (model_call: output_tokens) | `gen_ai.usage.output_tokens` | OTel GenAI semconv; set from `payload.usage.output_tokens` for `model_call` events. |
| `payload` (model_call: system) | `gen_ai.system` | OTel GenAI semconv; identifies the AI provider (e.g., `"openai"`, `"anthropic"`). |

---

## 4. CloudEvents v1.0 Mapping

When events are delivered over CloudEvents (e.g., via NATS CloudEvents bridge), the following mapping applies.

| NovaFabric field | CloudEvents attribute | Notes |
|---|---|---|
| `event_id` | `id` | CloudEvents `id` attribute. |
| `"nova://agent/{agent_id}"` | `source` | Constructed from `agent_id` at emission time. |
| `event_type` | `type` | CloudEvents `type` attribute (e.g., `"model_call"`). |
| `started_at` | `time` | CloudEvents `time` attribute (RFC 3339). |
| `"1.0"` | `specversion` | Always `"1.0"` for CloudEvents v1.0. |
| `trace_id` + `span_id` | `traceparent` (extension) | W3C traceparent: `00-{trace_id}-{span_id}-01`. Carried as a CloudEvents extension attribute. |

---

## 5. Canonical Encoding (ADR-001)

NovaSeal signs batches of OTLP `ExportLogsServiceRequest` messages. To produce a stable byte sequence for signing, the following deterministic pre-pass must be applied before marshalling:

1. Strip `nova.batch.signature` and `nova.batch.signing_key_id` from `Resource.attributes` (these are not signed — they are added after signing).
2. Sort `Resource.attributes` by key in lexicographic ascending order.
3. Sort each `LogRecord.attributes` by key in lexicographic ascending order.
4. Preserve `LogRecord` list order (records must not be reordered).
5. Marshal with `proto.MarshalOptions{Deterministic: true}` (Go protobuf v2).

Verifiers must re-apply this pre-pass before verifying: receive the signed batch, strip the signature fields, sort attributes as above, re-marshal, then verify the Ed25519 signature over the resulting bytes.

---

## 6. Downstream Consumers

The following Phase 3–6 components consume fields from this envelope. Each consumes only the fields listed and must tolerate any additional fields.

| Component | Fields consumed | Notes |
|---|---|---|
| `novafabric-parent-child-capsule` | `global_run_id`, `parent_run_id` | Builds parent→child edges in the capsule hierarchy. `parent_run_id = null` identifies root (PARENT/STANDALONE) capsules. |
| `novafabric-object-capsule-store` | `event_id` | Used as the content-addressable storage key (ULID encodes timestamp for sharding). |
| `novafabric-metadata-database` | `agent_id`, `cluster_id`, `tenant_id`, `event_type`, `started_at` | Indexed for query: filter by agent, cluster, tenant, event kind, time range. |
| `novafabric-lineage-at-scale` | `trace_id`, `span_id` | Correlates with OTel spans to build the lineage DAG. |

---

## 7. Versioning

**v1.x — additive only.** New optional fields may be added to the JSON Schema without incrementing the major version. Existing required fields and their types/constraints must not change. Consumers must tolerate unknown fields.

**v2 — breaking changes only.** Any change that removes a required field, changes a field's type, changes a constraint on an existing field, or changes the canonical encoding (Section 5) requires a new major version with a new `$id` and a new schema pin.

**Version pin.** The file `envelope-v1.sha256` in this directory contains the SHA-256 of `envelope-v1.json` at the time this version was frozen. Downstream case studies and CI fixtures pin against this hash. If a v1.x additive update is made, the hash file must be updated and downstream consumers must be re-tested.
