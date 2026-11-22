"""AWS Bedrock ``Converse`` / ``ConverseStream`` responses for the urllib3 wire hook.

``boto3`` sends every Bedrock runtime call through ``urllib3`` (botocore's
``URLLib3Session``), and the URL registry classifies ``bedrock-runtime.*`` hosts as
``gen_ai.system: aws.bedrock`` -- but the hook recorded no response
(``gen_ai.response.choices: []``) and ``gen_ai.request.model: "unknown"`` (the model
id is in the URL path), so mocked replay had nothing to serve. This module maps a
Converse exchange onto the canonical model-call record:

* request: ``gen_ai.request.model`` from ``/model/<modelId>/converse[-stream]``
  (URL-decoded), ``inferenceConfig`` onto the semconv request attributes, the
  ``messages`` verbatim (Bedrock content blocks), and ``system`` / ``toolConfig`` /
  ``additionalModelRequestFields`` verbatim under ``io.novafabric.bedrock.request``;
* response: ``gen_ai.response.choices[0].message`` -- role, the concatenated text
  blocks, ``toolUse`` blocks as canonical ``{id, name, arguments}`` refs (``id`` =
  ``toolUseId``); ``finish_reason`` from ``stopReason`` (raw value kept in
  ``io.novafabric.provider_finish_reasons``); usage from ``usage``;
* replay fidelity (all additive, optional extensions): the block layout
  (``io.novafabric.bedrock.blocks``), the rest of the response body verbatim
  (``io.novafabric.bedrock.final``), the event sequence of a stream with text
  replaced by lengths (``io.novafabric.bedrock.events``), a recorded HTTP error
  body (``io.novafabric.bedrock.http_error``) and a short allowlist of response
  headers (``io.novafabric.bedrock.response_headers``).

The SigV4 ``Authorization`` / ``X-Amz-*`` request headers are never read or
recorded. Pure functions and small classes; no I/O. The hook decides when (when
the workload's own read of the body ends) -- see ``_urllib3``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

from novafabric.capture.hooks._aws_eventstream import Decoder, Message
from novafabric.capture.hooks._finish_reason import attach_provider_finish_reasons
from novafabric.capture.hooks._ollama import RESPONSE_NOT_RECORDED_EXT
from novafabric.capture.hooks._sdk_streams import API_SURFACE_EXT, attach_stream_info
from novafabric.capture.hooks._tool_call_refs import (
    note_dropped_tool_calls,
    parse_tool_arguments,
)

BEDROCK_SYSTEM = "aws.bedrock"
BEDROCK_CONVERSE_SURFACE = "aws.bedrock.converse"
BEDROCK_CONVERSE_STREAM_SURFACE = "aws.bedrock.converse_stream"

#: Reverse-DNS extension keys (all additive and optional).
BEDROCK_REQUEST_EXT = "io.novafabric.bedrock.request"
BEDROCK_BLOCKS_EXT = "io.novafabric.bedrock.blocks"
BEDROCK_FINAL_EXT = "io.novafabric.bedrock.final"
BEDROCK_EVENTS_EXT = "io.novafabric.bedrock.events"
BEDROCK_HTTP_ERROR_EXT = "io.novafabric.bedrock.http_error"
BEDROCK_HEADERS_EXT = "io.novafabric.bedrock.response_headers"

#: ``/model/<modelId>/<action>`` -- the runtime paths that run a model.
_MODEL_PATH = re.compile(r"^/model/([^/]+)/([a-z-]+)/?$")
_SURFACE_ACTIONS: dict[str, str] = {
    "converse": BEDROCK_CONVERSE_SURFACE,
    "converse-stream": BEDROCK_CONVERSE_STREAM_SURFACE,
}
_INVOKE_ACTIONS = frozenset({"invoke", "invoke-with-response-stream"})
INVOKE_NOT_RECORDED = (
    "InvokeModel request and response bodies are model-provider specific; mocked "
    "replay serves Bedrock Converse / ConverseStream only, so the response is not recorded"
)

#: A response body (JSON, or an event stream) larger than this is not recorded.
MAX_BODY_BYTES = 64 * 1024 * 1024
#: Verbatim JSON kept beside the canonical record (request extras, the body's
#: non-content fields, an error body) is dropped above this size.
MAX_VERBATIM_BYTES = 256 * 1024
#: Stream events kept in ``BEDROCK_EVENTS_EXT``; past it the map is dropped and
#: replay serves a standard event sequence from the blocks.
MAX_EVENT_MAP = 10_000
#: Response headers recorded (in order) so replay serves them back:
#: ``ResponseMetadata.HTTPHeaders`` / ``RequestId`` in botocore.
_RECORDED_HEADERS = frozenset(
    {"date", "content-type", "content-length", "connection", "x-amzn-requestid",
     "x-amzn-errortype"}
)
_MAX_HEADERS = 32
_MAX_HEADER_VALUE = 1024

#: Bedrock ``stopReason`` -> model-call schema enum (unknown values -> ``stop``,
#: raw value kept beside it, as for Anthropic).
_STOP_TO_CANONICAL: dict[str, str] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "guardrail_intervened": "content_filter",
    "content_filtered": "content_filter",
}
_REQUEST_EXTRAS = ("system", "toolConfig", "additionalModelRequestFields")
#: Bedrock's anti-side-channel padding field on stream event payloads: random
#: characters, never recorded.
_PADDING = "p"


def _path(url: str) -> str:
    try:
        return urlsplit(url).path
    except ValueError:
        return ""


def model_path(url: str) -> tuple[str, str] | None:
    """``(modelId, action)`` of a runtime ``/model/<id>/<action>`` URL, URL-decoded."""
    match = _MODEL_PATH.match(_path(url))
    if match is None:
        return None
    return unquote(match.group(1)), match.group(2)


def bedrock_surface(url: str, gen_ai_system: str) -> str | None:
    """``aws.bedrock.converse`` / ``aws.bedrock.converse_stream``, else ``None``."""
    if gen_ai_system != BEDROCK_SYSTEM:
        return None
    parsed = model_path(url)
    return _SURFACE_ACTIONS.get(parsed[1]) if parsed else None


def is_invoke(url: str, gen_ai_system: str) -> bool:
    parsed = model_path(url) if gen_ai_system == BEDROCK_SYSTEM else None
    return parsed is not None and parsed[1] in _INVOKE_ACTIONS


def _bounded(value: Any) -> Any:
    """*value* when its JSON form is at most :data:`MAX_VERBATIM_BYTES`, else ``None``."""
    try:
        size = len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return None
    return value if size <= MAX_VERBATIM_BYTES else None


def request_body(body: dict[str, Any], url: str) -> dict[str, Any]:
    """An OpenAI-shaped copy of a Converse request for
    :func:`~novafabric.capture.hooks._otel_genai.extract_request_attributes`:
    ``model`` from the URL, ``inferenceConfig`` keys as top-level keys."""
    out: dict[str, Any] = {"messages": body.get("messages") or []}
    parsed = model_path(url)
    if parsed is not None:
        out["model"] = parsed[0]
    config = body.get("inferenceConfig")
    if isinstance(config, dict):
        for src, dst in (("maxTokens", "max_tokens"), ("temperature", "temperature"),
                         ("topP", "top_p"), ("stopSequences", "stop_sequences")):
            if src in config:
                out[dst] = config[src]
    return out


def request_extension(body: dict[str, Any]) -> dict[str, Any] | None:
    """``system`` / ``toolConfig`` / ``additionalModelRequestFields``, verbatim."""
    extras = {k: body[k] for k in _REQUEST_EXTRAS if k in body}
    if not extras:
        return None
    kept = _bounded(extras)
    return kept if kept is not None else {"omitted": f"larger than {MAX_VERBATIM_BYTES} bytes"}


def recorded_headers(headers: Any) -> list[list[str]]:
    """The allowlisted response headers, in order, as ``[name, value]`` pairs."""
    out: list[list[str]] = []
    try:
        items = list(headers.items()) if headers is not None else []
    except Exception:  # noqa: BLE001 -- a header map that cannot be listed records none
        return out
    for name, value in items:
        if len(out) >= _MAX_HEADERS:
            break
        if str(name).lower() in _RECORDED_HEADERS and len(str(value)) <= _MAX_HEADER_VALUE:
            out.append([str(name), str(value)])
    return out


def canonical_stop(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    return _STOP_TO_CANONICAL.get(raw, "stop")


def _without_padding(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {k: v for k, v in payload.items() if k != _PADDING}
    return payload


# ── folding a response ──────────────────────────────────────────────────────


@dataclass
class _Block:
    kind: str  # "text" | "toolUse" | "other"
    index: int = 0
    text: str = ""
    tool: dict[str, Any] = field(default_factory=dict)
    raw_input: str = ""
    other: Any = None


@dataclass
class Folded:
    """A Converse response folded into the parts the record needs."""

    role: str = "assistant"
    blocks: list[_Block] = field(default_factory=list)
    stop_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    final: dict[str, Any] | None = None
    events: list[list[Any]] | None = None
    #: An in-stream exception frame (``:message-type: exception``).
    exception: dict[str, Any] | None = None
    delivered: int = 0


def fold_converse(body: dict[str, Any]) -> Folded:
    """A non-streaming ``Converse`` response body."""
    folded = Folded()
    output = body.get("output")
    message = output.get("message") if isinstance(output, dict) else None
    message = message if isinstance(message, dict) else {}
    if isinstance(message.get("role"), str):
        folded.role = message["role"]
    for block in message.get("content") or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            folded.blocks.append(_Block("text", text=block["text"]))
        elif isinstance(block, dict) and isinstance(block.get("toolUse"), dict):
            folded.blocks.append(_Block("toolUse", tool=dict(block["toolUse"])))
        else:
            folded.blocks.append(_Block("other", other=block))
    folded.stop_reason = body.get("stopReason") if isinstance(body.get("stopReason"), str) else None
    if isinstance(body.get("usage"), dict):
        folded.usage = body["usage"]
    final = json.loads(json.dumps(body))
    if isinstance(final.get("output"), dict) and isinstance(final["output"].get("message"), dict):
        # The content is carried by the canonical message and the block map.
        final["output"]["message"]["content"] = None
    folded.final = _bounded(final)
    folded.delivered = 1
    return folded


class StreamFolder:
    """Fold ``ConverseStream`` event-stream frames as they are decoded."""

    def __init__(self) -> None:
        self.folded = Folded(events=[])
        self._by_index: dict[int, _Block] = {}

    def _block(self, index: Any, kind: str) -> _Block:
        key = index if isinstance(index, int) else len(self._by_index)
        block = self._by_index.get(key)
        if block is None:
            block = _Block(kind, index=key)
            self._by_index[key] = block
        elif block.kind == "text" and kind != "text" and not block.text:
            block.kind = kind
        return block

    def _event(self, entry: list[Any]) -> None:
        events = self.folded.events
        if events is None:
            return
        if len(events) >= MAX_EVENT_MAP:
            self.folded.events = None
            return
        events.append(entry)

    def add(self, message: Message) -> None:
        self.folded.delivered += 1
        kind = message.headers.get(":message-type")
        if kind == "exception" or kind == "error":
            text = message.payload.decode("utf-8", "replace")
            headers = {k: v for k, v in message.headers.items() if isinstance(v, str)}
            self.folded.exception = {"headers": headers, "payload": text}
            self._event(["exception", headers, text])
            return
        event_type = message.headers.get(":event-type")
        try:
            payload = _without_padding(json.loads(message.payload or b"{}"))
        except ValueError:
            payload = None
        if not isinstance(event_type, str) or not isinstance(payload, dict):
            self._event(["raw", {k: v for k, v in message.headers.items()
                                 if isinstance(v, str)},
                         message.payload.decode("utf-8", "replace")])
            return
        self._apply(event_type, payload)

    def _apply(self, event_type: str, payload: dict[str, Any]) -> None:
        folded = self.folded
        if event_type == "messageStart":
            if isinstance(payload.get("role"), str):
                folded.role = payload["role"]
        elif event_type == "contentBlockStart":
            start = payload.get("start")
            tool = start.get("toolUse") if isinstance(start, dict) else None
            if isinstance(tool, dict):
                self._block(payload.get("contentBlockIndex"), "toolUse").tool = dict(tool)
        elif event_type == "contentBlockDelta":
            if self._delta(payload):
                return
        elif event_type == "messageStop":
            raw = payload.get("stopReason")
            folded.stop_reason = raw if isinstance(raw, str) else None
        elif event_type == "metadata" and isinstance(payload.get("usage"), dict):
            folded.usage = payload["usage"]
        self._event([event_type, payload])

    def _delta(self, payload: dict[str, Any]) -> bool:
        """Fold a delta; True when it was recorded as a length (text, tool input)."""
        index = payload.get("contentBlockIndex")
        delta = payload.get("delta")
        if not (isinstance(index, int) and isinstance(delta, dict) and len(delta) == 1):
            return False
        if isinstance(delta.get("text"), str):
            block = self._block(index, "text")
            block.text += delta["text"]
            self._event(["delta", index, "text", len(delta["text"])])
            return True
        tool = delta.get("toolUse")
        if isinstance(tool, dict) and set(tool) == {"input"} and isinstance(tool["input"], str):
            block = self._block(index, "toolUse")
            block.raw_input += tool["input"]
            self._event(["delta", index, "toolUse", len(tool["input"])])
            return True
        self._block(index, "other")
        return False

    def result(self) -> Folded:
        self.folded.blocks = [self._by_index[k] for k in sorted(self._by_index)]
        return self.folded


# ── the record ──────────────────────────────────────────────────────────────


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _tool_refs(
    folded: Folded, streamed: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Canonical refs, the block map, and how many nameless tool blocks were dropped.

    Block map entries (``BEDROCK_BLOCKS_EXT``), in content order: ``{"type":
    "text", "chars": n}`` (a slice of the canonical text), ``{"type": "toolUse",
    "call": k}`` (the k-th canonical tool call), ``{"type": "other", "block":
    {...}}`` (a block the canonical message cannot hold, verbatim; ``null`` when too
    large). A streamed record's entries add ``index`` (the ``contentBlockIndex``) and
    a tool block its raw ``input`` JSON text, as streamed."""
    refs: list[dict[str, Any]] = []
    layout: list[dict[str, Any]] = []
    dropped = 0
    for block in folded.blocks:
        entry: dict[str, Any]
        if block.kind == "text":
            entry = {"type": "text", "chars": len(block.text)}
        elif block.kind == "toolUse" and isinstance(block.tool.get("name"), str) \
                and block.tool["name"]:
            raw = block.raw_input if streamed else block.tool.get("input")
            refs.append({
                "id": str(block.tool.get("toolUseId") or ""),
                "name": block.tool["name"],
                "arguments": parse_tool_arguments(raw),
            })
            entry = {"type": "toolUse", "call": len(refs) - 1}
            if streamed:
                entry["input"] = block.raw_input
        else:
            if block.kind == "toolUse":
                dropped += 1
            entry = {"type": "other"}
            if not streamed:
                entry["block"] = _bounded(block.other if block.kind == "other"
                                          else {"toolUse": block.tool})
        if streamed:
            entry["index"] = block.index
        layout.append(entry)
    return refs, layout, dropped


def apply_response(
    record: dict[str, Any],
    surface: str,
    folded: Folded | None,
    *,
    headers: list[list[str]] | None = None,
    http_error: dict[str, Any] | None = None,
    not_recorded: str | None = None,
    complete: bool = True,
    first_ms: int | None = None,
) -> None:
    """Write the response side of a Bedrock record (in place). Never raises for
    well-typed inputs; the caller wraps it in ``fail_open``."""
    extensions = record.setdefault("extensions", {})
    extensions[API_SURFACE_EXT] = surface
    if headers:
        extensions[BEDROCK_HEADERS_EXT] = headers
    if http_error is not None:
        record["status"] = "error"
        record["error"] = {"type": http_error["error_type"] or "HTTPError",
                           "message": http_error["message"], "traceback_ref": None}
        if http_error.get("body") is not None:
            extensions[BEDROCK_HTTP_ERROR_EXT] = {
                "status": http_error["status"], "body": http_error["body"],
            }
        else:
            extensions[RESPONSE_NOT_RECORDED_EXT] = "the error body was not recorded"
        return
    if not_recorded is not None or folded is None:
        extensions[RESPONSE_NOT_RECORDED_EXT] = not_recorded or "no response body was recorded"
        return
    streamed = surface == BEDROCK_CONVERSE_STREAM_SURFACE
    refs, layout, dropped = _tool_refs(folded, streamed)
    finish = canonical_stop(folded.stop_reason)
    if finish is not None and refs and finish == "stop":
        finish = "tool_calls"
    if folded.delivered:
        message: dict[str, Any] = {
            "role": folded.role,
            "content": "".join(b.text for b in folded.blocks if b.kind == "text"),
        }
        if refs:
            message["tool_calls"] = refs
        record["gen_ai.response.choices"] = [
            {"index": 0, "message": message, "finish_reason": finish}
        ]
        extensions[BEDROCK_BLOCKS_EXT] = layout
    if finish is not None:
        record["gen_ai.response.finish_reasons"] = [finish]
        attach_provider_finish_reasons(record, [str(folded.stop_reason)], [finish])
    record["gen_ai.usage.input_tokens"] = _count(folded.usage.get("inputTokens"))
    record["gen_ai.usage.output_tokens"] = _count(folded.usage.get("outputTokens"))
    if folded.final is not None:
        extensions[BEDROCK_FINAL_EXT] = folded.final
    note_dropped_tool_calls(record, dropped)
    if streamed:
        attach_stream_info(record, (folded.delivered, first_ms, complete))
        if folded.events is not None:
            extensions[BEDROCK_EVENTS_EXT] = folded.events
    if folded.exception is not None:
        headers_ = folded.exception["headers"]
        try:
            message_text = str(json.loads(folded.exception["payload"]).get("message", ""))
        except (ValueError, AttributeError):
            message_text = folded.exception["payload"][:1024]
        record["status"] = "error"
        record["error"] = {
            "type": str(headers_.get(":exception-type") or headers_.get(":error-code")
                        or "EventStreamError"),
            "message": message_text, "traceback_ref": None,
        }


def http_error_of(status: int, headers: Any, body: bytes | None) -> dict[str, Any]:
    """The error a Bedrock HTTP error response carries (botocore's view of it)."""
    error_type = ""
    try:
        raw_type = headers.get("x-amzn-ErrorType") if headers is not None else None
    except Exception:  # noqa: BLE001
        raw_type = None
    if isinstance(raw_type, str):
        error_type = raw_type.split(":", 1)[0]
    text: str | None = None
    message = f"HTTP {status}"
    if body is not None and len(body) <= MAX_VERBATIM_BYTES:
        text = body.decode("utf-8", "replace")
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            message = str(parsed.get("message") or parsed.get("Message") or message)
            if not error_type and isinstance(parsed.get("__type"), str):
                error_type = parsed["__type"].rsplit("#", 1)[-1]
    return {"status": status, "error_type": error_type, "message": message, "body": text}


class BodySink:
    """Where a Bedrock response body's bytes go as the workload reads them.

    A JSON body (``Converse``, or any error response) is buffered and parsed when
    the read ends; an event stream (``ConverseStream`` with status 200) is decoded
    and folded frame by frame. Bounded (:data:`MAX_BODY_BYTES`); never raises.
    """

    def __init__(self, surface: str, status: int, encoding: str) -> None:
        self.surface = surface
        self.status = status
        self.not_recorded: str | None = None
        if encoding not in ("", "identity"):
            self.not_recorded = f"content-encoding {encoding!r} is not decoded by capture"
        self._events = surface == BEDROCK_CONVERSE_STREAM_SURFACE and 200 <= status < 300
        self._decoder = Decoder()
        self._folder = StreamFolder()
        self._buffer = bytearray()
        self._size = 0

    def feed(self, data: Any) -> None:
        if self.not_recorded is not None or not data:
            return
        try:
            chunk = bytes(data)
            self._size += len(chunk)
            if self._size > MAX_BODY_BYTES:
                self.not_recorded = f"the response body exceeded {MAX_BODY_BYTES} bytes"
                self._buffer.clear()
                return
            if not self._events:
                self._buffer.extend(chunk)
                return
            for message in self._decoder.feed(chunk):
                self._folder.add(message)
            if self._decoder.failed is not None:
                self.not_recorded = self._decoder.failed
        except Exception:  # noqa: BLE001 -- capture must never fail the workload
            self.not_recorded = "the response body could not be parsed"

    def body(self) -> bytes:
        return bytes(self._buffer)

    def folded(self) -> Folded | None:
        """The folded response, or ``None`` (with :attr:`not_recorded` set)."""
        if self.not_recorded is not None:
            return None
        if self._events:
            return self._folder.result()
        try:
            parsed = json.loads(self._buffer or b"{}")
        except ValueError:
            self.not_recorded = "the response body is not JSON"
            return None
        if not isinstance(parsed, dict):
            self.not_recorded = "the response body is not a JSON object"
            return None
        return fold_converse(parsed)
