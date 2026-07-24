# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""OTLP/HTTP JSON → run-capsule ingest for OTel GenAI spans (NF-034, ADR-0098).

The inbound half of the ADR-0098 canonical vocabulary. :mod:`genai_emitter` maps
a capsule *outward* to OTel GenAI spans; this module maps an OTLP/HTTP **JSON**
``ExportTraceServiceRequest``-shaped payload (``resourceSpans`` → ``scopeSpans``
→ ``spans``) *back* into capsule event dicts (``model-calls.jsonl`` /
``tool-calls.jsonl``) and seals them into a minimal valid run capsule — so agents
instrumented with vanilla OTel GenAI SDKs can land evidence in NovaFabric
without the capture orchestrator.

Honesty rules (ADR-0021 §4, ADR-0009):

- Only spans carrying at least one ``gen_ai.*`` attribute become capsule events;
  every other span is counted as skipped — never guessed at.
- Message/choice **content** is carried over only when present in the span;
  nothing is fabricated.
- Unknown ``gen_ai.*`` / ``novafabric.*`` attributes are preserved verbatim
  under the event's ``otlp.unmapped`` key and enumerated in the result;
  other-namespace span attributes are dropped but their keys are enumerated.
- Every event is stamped with the same ``novafabric.mapping_version`` the
  emitter uses, so consumers can tell which capsule↔OTLP mapping produced it.
- The sealed capsule records ``capture_mode: otel-import`` and
  ``metadata.capture_level: ingested-otlp`` — honestly lower-fidelity than
  native capture. Ingested text passes through the ADR-0009 secret scanner
  before the manifest is written.

OTLP/**protobuf** ingest is also supported (ADR-0177) via
:func:`parse_otlp_protobuf` / :func:`ingest_otlp_protobuf`, which decode the
binary ``ExportTraceServiceRequest`` and reuse the JSON path — so both wire
encodings converge on identical events. Protobuf decoding requires the ``otlp``
extra (``pip install 'novafabric[otlp]'``, opentelemetry-proto, Apache-2.0).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novafabric.otel.genai_emitter import MAPPING_VERSION
from novafabric.otel.openinference import translate_attributes as translate_openinference

#: capture-level label recorded on every ingested capsule (ADR-0021 §4).
CAPTURE_LEVEL = "ingested-otlp"


class OTLPIngestError(ValueError):
    """Raised when a payload is not a decodable OTLP/HTTP JSON trace export."""


# ── attribute mapping tables (inverse of genai_emitter) ──────────────────────

#: model-call attributes copied through verbatim (Stable client-span subset
#: plus the request parameters the wire hooks record).
_MODEL_KEYS = frozenset({
    "gen_ai.system",
    "gen_ai.operation.name",
    "gen_ai.request.model",
    "gen_ai.response.model",
    "gen_ai.response.id",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.response.finish_reasons",
    "gen_ai.request.temperature",
    "gen_ai.request.top_p",
    "gen_ai.request.top_k",
    "gen_ai.request.max_tokens",
    "gen_ai.request.frequency_penalty",
    "gen_ai.request.presence_penalty",
    "gen_ai.request.seed",
    "gen_ai.request.stop_sequences",
    "gen_ai.request.choice.count",
})

#: content attributes — ingested **only when present** (ADR-0021: never fabricate).
_CONTENT_KEYS = frozenset({
    "gen_ai.request.messages",
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.response.choices",
    "gen_ai.system_instructions",
})

#: tool-call attributes copied through verbatim (Development semconv subset).
_TOOL_KEYS = frozenset({
    "gen_ai.tool.name",
    "gen_ai.tool.call.id",
    "gen_ai.tool.description",
    "gen_ai.tool.type",
})

#: agent-span attributes used for manifest metadata.
_AGENT_KEYS = frozenset({
    "gen_ai.agent.id",
    "gen_ai.agent.name",
    "gen_ai.agent.description",
})

#: NovaFabric markers stamped by our own emitter — recognized, not "unknown".
_MARKER_KEYS = frozenset({
    "novafabric.mapping_version",
    "novafabric.semconv_maturity",
    "novafabric.content.truncated",
})

_KNOWN_KEYS = _MODEL_KEYS | _CONTENT_KEYS | _TOOL_KEYS | _AGENT_KEYS | _MARKER_KEYS

_MODEL_OPERATIONS = frozenset({"chat", "text_completion", "generate_content", "embeddings"})
_AGENT_OPERATIONS = frozenset({"invoke_agent", "create_agent"})


# ── OTLP JSON decoding ────────────────────────────────────────────────────────


def _decode_any_value(value: Any) -> Any:
    """Decode one OTLP JSON ``AnyValue``; plain (non-wrapped) values pass through."""
    if not isinstance(value, dict):
        return value
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        try:
            return int(value["intValue"])
        except (TypeError, ValueError):
            return value["intValue"]
    if "doubleValue" in value:
        return value["doubleValue"]
    if "boolValue" in value:
        return value["boolValue"]
    if "arrayValue" in value:
        inner = value["arrayValue"]
        values = inner.get("values", []) if isinstance(inner, dict) else []
        return [_decode_any_value(v) for v in values]
    if "kvlistValue" in value:
        inner = value["kvlistValue"]
        values = inner.get("values", []) if isinstance(inner, dict) else []
        return {
            str(kv.get("key", "")): _decode_any_value(kv.get("value"))
            for kv in values
            if isinstance(kv, dict)
        }
    if "bytesValue" in value:
        return value["bytesValue"]  # base64 string, passed through undecoded
    return value  # plain dict (e.g. emitted by genai_emitter) — keep as-is


def _decode_attributes(raw: Any) -> dict[str, Any]:
    """Decode OTLP JSON attribute lists; accept plain dicts too (round-trip)."""
    if isinstance(raw, dict):
        return {str(k): _decode_any_value(v) for k, v in raw.items()}
    out: dict[str, Any] = {}
    if isinstance(raw, list):
        for kv in raw:
            if isinstance(kv, dict) and "key" in kv:
                out[str(kv["key"])] = _decode_any_value(kv.get("value"))
    return out


def _to_nanos(value: Any) -> int:
    """OTLP JSON encodes int64 nanos as strings; accept int or str, 0 on absence."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _iso(nanos: int) -> str | None:
    """Unix nanoseconds → ISO-8601 UTC (inverse of the emitter's ``_unix_nano``)."""
    if nanos <= 0:
        return None
    secs, rem = divmod(nanos, 1_000_000_000)
    dt = datetime.fromtimestamp(secs, tz=timezone.utc).replace(microsecond=rem // 1000)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _is_error(status: Any) -> bool:
    if not isinstance(status, dict):
        return False
    return status.get("code") in (2, "2", "STATUS_CODE_ERROR")


def parse_otlp_json(payload: Any) -> list[dict[str, Any]]:
    """Flatten an OTLP/HTTP JSON trace export into normalized span dicts.

    Pure function. Each returned dict has ``name``, ``trace_id``, ``span_id``,
    ``parent_span_id``, ``start_unix_nano``, ``end_unix_nano``, ``error``,
    ``status_message`` and a decoded ``attributes`` mapping.

    Raises :class:`OTLPIngestError` when the payload is not
    ``ExportTraceServiceRequest``-shaped (``resourceSpans`` → ``scopeSpans`` →
    ``spans``).
    """
    if not isinstance(payload, dict):
        raise OTLPIngestError("OTLP payload must be a JSON object")
    resource_spans = payload.get("resourceSpans")
    if not isinstance(resource_spans, list):
        raise OTLPIngestError("OTLP payload must carry a 'resourceSpans' array")

    spans: list[dict[str, Any]] = []
    for block in resource_spans:
        if not isinstance(block, dict):
            raise OTLPIngestError("each resourceSpans entry must be an object")
        scope_spans = block.get("scopeSpans", [])
        if not isinstance(scope_spans, list):
            raise OTLPIngestError("'scopeSpans' must be an array")
        for scope_block in scope_spans:
            if not isinstance(scope_block, dict):
                raise OTLPIngestError("each scopeSpans entry must be an object")
            raw_spans = scope_block.get("spans", [])
            if not isinstance(raw_spans, list):
                raise OTLPIngestError("'spans' must be an array")
            for raw in raw_spans:
                if not isinstance(raw, dict):
                    raise OTLPIngestError("each span must be an object")
                status = raw.get("status")
                spans.append({
                    "name": str(raw.get("name", "")),
                    "trace_id": str(raw.get("traceId", "")),
                    "span_id": str(raw.get("spanId", "")),
                    "parent_span_id": str(raw.get("parentSpanId", "")) or None,
                    "start_unix_nano": _to_nanos(raw.get("startTimeUnixNano")),
                    "end_unix_nano": _to_nanos(raw.get("endTimeUnixNano")),
                    "error": _is_error(status),
                    "status_message": (
                        str(status.get("message", "")) if isinstance(status, dict) else ""
                    ),
                    "attributes": _decode_attributes(raw.get("attributes")),
                })
    return spans


# ── span → capsule-event mapping ─────────────────────────────────────────────


@dataclass
class GenAIIngestResult:
    """Outcome of mapping an OTLP payload to capsule events (pure data)."""

    model_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    #: manifest metadata from the first ``invoke_agent``/``create_agent`` span.
    agent: dict[str, Any] | None = None
    #: total spans in the payload, GenAI or not.
    spans_seen: int = 0
    #: agent-operation spans (consumed as manifest metadata, not events).
    agent_span_count: int = 0
    #: spans with no ``gen_ai.*`` attribute at all — never guessed at.
    skipped_spans: int = 0
    #: GenAI spans that fit no known operation shape — skipped honestly.
    unclassified_spans: int = 0
    #: unknown ``gen_ai.*``/``novafabric.*`` keys carried under ``otlp.unmapped``.
    unmapped_keys: list[str] = field(default_factory=list)
    #: other-namespace span-attribute keys not ingested (enumerated, not silent).
    dropped_keys: list[str] = field(default_factory=list)

    @property
    def genai_spans(self) -> int:
        """GenAI spans that were actually consumed (events + agent metadata)."""
        return len(self.model_calls) + len(self.tool_calls) + self.agent_span_count


def _base_event(span: dict[str, Any]) -> dict[str, Any]:
    event: dict[str, Any] = {}
    started = _iso(span["start_unix_nano"])
    finished = _iso(span["end_unix_nano"])
    if started:
        event["started_at"] = started
    if finished:
        event["finished_at"] = finished
    if span["start_unix_nano"] > 0 and span["end_unix_nano"] >= span["start_unix_nano"]:
        event["duration_ms"] = (span["end_unix_nano"] - span["start_unix_nano"]) // 1_000_000
    event["status"] = "error" if span["error"] else "success"
    if span["error"]:
        event["error"] = {
            "type": "OTLPStatusError",
            "message": span["status_message"] or "span reported STATUS_CODE_ERROR",
            "traceback_ref": None,
        }
        # ADR-0127 inbound span-status mapping: an ERROR span records
        # log_level=error (OK/UNSET set nothing from the span alone).
        event["log_level"] = "error"
        event["log_level_source"] = "span-status"
        if span["status_message"]:
            event["status_message"] = span["status_message"]
    event["novafabric.mapping_version"] = MAPPING_VERSION
    return event


def _split_unknown(
    attrs: dict[str, Any], result: GenAIIngestResult
) -> dict[str, Any]:
    """Partition unknown attributes: gen_ai./novafabric. carried, rest enumerated."""
    unmapped: dict[str, Any] = {}
    for key, value in attrs.items():
        if key in _KNOWN_KEYS:
            continue
        if key.startswith(("gen_ai.", "novafabric.")):
            unmapped[key] = value
            if key not in result.unmapped_keys:
                result.unmapped_keys.append(key)
        elif key not in result.dropped_keys:
            result.dropped_keys.append(key)
    return unmapped


def _classify(attrs: dict[str, Any]) -> str:
    op = attrs.get("gen_ai.operation.name")
    if op == "execute_tool" or "gen_ai.tool.name" in attrs:
        return "tool"
    if op in _AGENT_OPERATIONS or attrs.keys() & _AGENT_KEYS:
        return "agent"
    if op in _MODEL_OPERATIONS or "gen_ai.request.model" in attrs or (
        "gen_ai.response.model" in attrs
    ):
        return "model"
    return "unclassified"


def ingest_otlp_json(payload: Any) -> GenAIIngestResult:
    """Map an OTLP/HTTP JSON trace export to capsule event dicts (pure function).

    Filters spans carrying ``gen_ai.*`` attributes and inverts the
    :mod:`genai_emitter` mapping: ``chat``-family client spans become
    model-call events, ``execute_tool`` spans become tool-call events, and the
    first ``invoke_agent``/``create_agent`` span provides manifest metadata.

    Raises :class:`OTLPIngestError` on a malformed payload.
    """
    result = GenAIIngestResult()
    for span in parse_otlp_json(payload):
        result.spans_seen += 1
        attrs = span["attributes"]
        # ADR-0098: translate the OpenInference vocabulary (LangChain,
        # LlamaIndex, CrewAI, Phoenix) into gen_ai.* BEFORE the filter below,
        # so those spans take the identical classification and passthrough
        # path as natively-emitted ones. Non-OpenInference spans pass through
        # untouched.
        attrs = translate_openinference(attrs)
        if not any(k.startswith("gen_ai.") for k in attrs):
            result.skipped_spans += 1
            continue

        kind = _classify(attrs)
        if kind == "unclassified":
            result.unclassified_spans += 1
            continue

        if kind == "agent":
            result.agent_span_count += 1
            if result.agent is None:
                agent_name = attrs.get("gen_ai.agent.name")
                result.agent = {
                    "agent_name": str(agent_name) if agent_name else span["name"],
                    "provider": attrs.get("gen_ai.system"),
                }
            _split_unknown(attrs, result)
            continue

        event = _base_event(span)
        passthrough = _MODEL_KEYS | _CONTENT_KEYS if kind == "model" else _TOOL_KEYS
        for key in sorted(passthrough & attrs.keys()):
            # Content keys land here only when the span carried them (ADR-0021).
            event[key] = attrs[key]
        unmapped = _split_unknown(attrs, result)
        if unmapped:
            event["otlp.unmapped"] = unmapped

        if kind == "tool":
            tool_name = attrs.get("gen_ai.tool.name")
            event["tool_name"] = str(tool_name) if tool_name else span["name"]
            result.tool_calls.append(event)
        else:
            event.setdefault("gen_ai.operation.name", "chat")
            result.model_calls.append(event)

    result.unmapped_keys.sort()
    result.dropped_keys.sort()
    return result


# ── OTLP/protobuf decoding (ADR-0177) ────────────────────────────────────────


def _b64_to_hex(value: Any) -> Any:
    """Convert a base64 string to lowercase hex; pass non-base64 through unchanged."""
    if not isinstance(value, str) or not value:
        return value
    import base64
    import binascii

    try:
        return base64.b64decode(value, validate=True).hex()
    except (binascii.Error, ValueError):
        return value  # already hex or malformed — leave as-is for parse_otlp_json


def _normalize_protobuf_ids(payload: dict[str, Any]) -> None:
    """Convert trace/span IDs from base64 (MessageToDict) to hex (OTLP/JSON), in place.

    ``google.protobuf.json_format.MessageToDict`` encodes proto ``bytes`` fields as
    base64, but the OTLP/JSON convention — which :func:`parse_otlp_json` follows —
    uses lowercase hex for ``traceId``/``spanId``/``parentSpanId``. Normalizing here
    lets both wire encodings produce identical normalized spans.
    """
    for rs in payload.get("resourceSpans", []) or []:
        for ss in rs.get("scopeSpans", []) or []:
            for sp in ss.get("spans", []) or []:
                for key in ("traceId", "spanId", "parentSpanId"):
                    if key in sp:
                        sp[key] = _b64_to_hex(sp[key])


def _protobuf_to_payload(data: bytes) -> dict[str, Any]:
    """Decode a binary OTLP ``ExportTraceServiceRequest`` into the OTLP/JSON dict shape."""
    try:
        from google.protobuf.json_format import MessageToDict
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )
    except ImportError as exc:  # pragma: no cover - import guard
        raise OTLPIngestError(
            "OTLP/protobuf ingest requires the 'otlp' extra: "
            "pip install 'novafabric[otlp]' (adds opentelemetry-proto, Apache-2.0)."
        ) from exc

    req = ExportTraceServiceRequest()
    try:
        req.ParseFromString(data)
    except Exception as exc:  # protobuf DecodeError and friends
        raise OTLPIngestError(
            f"not a decodable OTLP/protobuf trace export: {exc}"
        ) from exc

    payload: dict[str, Any] = MessageToDict(req)
    # MessageToDict omits empty repeated fields, so a zero-span export yields {}.
    # An empty OTLP request is valid and means "no spans" — normalize it so the
    # JSON parser sees an (empty) resourceSpans array instead of raising.
    payload.setdefault("resourceSpans", [])
    _normalize_protobuf_ids(payload)
    return payload


def parse_otlp_protobuf(data: bytes) -> list[dict[str, Any]]:
    """Flatten an OTLP/**protobuf** trace export into normalized span dicts (ADR-0177).

    Decodes the binary ``ExportTraceServiceRequest`` and reuses
    :func:`parse_otlp_json`, so the two wire encodings converge on identical
    normalized spans. Requires the ``otlp`` extra (opentelemetry-proto).
    """
    return parse_otlp_json(_protobuf_to_payload(data))


def ingest_otlp_protobuf(data: bytes) -> GenAIIngestResult:
    """Map an OTLP/**protobuf** trace export to capsule events (ADR-0177).

    Binary sibling of :func:`ingest_otlp_json`; both converge on identical events
    for the same spans. Requires the ``otlp`` extra (opentelemetry-proto).
    """
    return ingest_otlp_json(_protobuf_to_payload(data))


# ── capsule sealing ──────────────────────────────────────────────────────────


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def write_ingest_capsule(
    result: GenAIIngestResult, base_dir: Path, *, run_id: str | None = None
) -> Path:
    """Seal a :class:`GenAIIngestResult` into a minimal valid run capsule.

    Reuses the native capture utilities (``CapsuleWriter``, environment lock,
    ADR-0009 secret scanner, replay policy) so the capsule passes
    ``nova validate``. The manifest records ``capture_mode: otel-import`` and
    ``metadata.capture_level: ingested-otlp`` (honestly lower-fidelity than
    native capture, ADR-0021 §4). Returns the capsule directory.
    """
    from importlib.metadata import version as _pkg_version

    import yaml

    from novafabric.capture._ulid import new_span_id, new_ulid
    from novafabric.capture.capsule import CapsuleWriter
    from novafabric.capture.env import capture_environment
    from novafabric.capture.orchestrator import _build_host_info, _now
    from novafabric.capture.replay import minimal_replay_policy
    from novafabric.capture.secrets import SecretScannerV0

    run_id = run_id or new_ulid()
    events = result.model_calls + result.tool_calls
    starts = sorted(e["started_at"] for e in events if "started_at" in e)
    ends = sorted(e["finished_at"] for e in events if "finished_at" in e)
    now = _now()
    created_at = starts[0] if starts else now
    finished_at = ends[-1] if ends else created_at
    duration_ms = 0
    start_dt, end_dt = _parse_iso(created_at), _parse_iso(finished_at)
    if start_dt and end_dt and end_dt >= start_dt:
        duration_ms = int((end_dt - start_dt).total_seconds() * 1000)

    writer = CapsuleWriter(run_id, base_dir)
    writer.open()
    for record in result.model_calls:
        writer.append_model_call({"model_call_id": new_ulid(), **record})
    for record in result.tool_calls:
        writer.append_tool_call({"tool_call_id": new_ulid(), **record})

    root_span_id = new_span_id()
    writer.append_trace_span({
        "span_id": root_span_id,
        "parent_span_id": None,
        "name": "novafabric.otlp_ingest",
        "started_at": created_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "status": "ok",
        "attributes": {
            "capture_level": CAPTURE_LEVEL,
            "spans_seen": result.spans_seen,
            "spans_ingested": result.genai_spans,
            "spans_skipped": result.skipped_spans + result.unclassified_spans,
        },
    })

    writer.write_text(
        "env.lock", yaml.dump(capture_environment(created_at, run_id), allow_unicode=True)
    )

    # ADR-0009: ingested foreign span content passes the secret scanner too.
    import json as _json

    proof = SecretScannerV0(capsule_dir=writer.capsule_dir, run_id=run_id).scan_and_redact()
    writer.write_text("redaction-proof.json", _json.dumps(proof, indent=2))
    writer.write_text("replay.yaml", yaml.dump(minimal_replay_policy(), allow_unicode=True))

    any_error = any(e.get("status") == "error" for e in events)
    metadata = {
        "capture_level": CAPTURE_LEVEL,
        "otlp.mapping_version": MAPPING_VERSION,
        "otlp.spans_seen": str(result.spans_seen),
        "otlp.spans_ingested": str(result.genai_spans),
        "otlp.spans_skipped": str(result.skipped_spans + result.unclassified_spans),
    }
    if result.agent and result.agent.get("agent_name"):
        metadata["otlp.agent_name"] = str(result.agent["agent_name"])

    working_dir = str(Path.cwd()).replace(str(Path.home()), "~")
    manifest: dict[str, Any] = {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "created_at": created_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "status": "partial" if any_error else "success",
        "command": [],  # SDK/ingest-driven run — no argv (schema-sanctioned).
        "capture_mode": "otel-import",
        "novafabric_version": _pkg_version("novafabric"),
        "working_directory": working_dir,
        "host": _build_host_info(),
        "environment_ref": "env.lock",
        "replay_policy_ref": "replay.yaml",
        "redaction_proof_ref": "redaction-proof.json",
        "trace_ref": "trace.jsonl",
        "trace_root_span_id": root_span_id,
        "model_calls_ref": "model-calls.jsonl",
        "tool_calls_ref": "tool-calls.jsonl",
        "assets_ref": "assets.jsonl",
        "inputs": [],
        "outputs": [],
        "model_call_count": len(result.model_calls),
        "tool_call_count": len(result.tool_calls),
        "mutating_tool_count": 0,
        "metadata": metadata,
    }
    writer.write_text("capsule.yaml", yaml.dump(manifest, allow_unicode=True))
    return writer.capsule_dir
