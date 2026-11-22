"""Serving recorded AWS Bedrock Converse calls at the ``urllib3`` layer (experimental).

Mocked replay patches ``urllib3.connectionpool.HTTPConnectionPool.urlopen`` -- the
call botocore's ``URLLib3Session.send`` makes for every AWS request. A ``POST`` to
``/model/<modelId>/converse`` or ``/converse-stream`` on a host the URL registry
classifies as ``aws.bedrock`` (``bedrock-runtime.*``, or
``AWS_ENDPOINT_URL_BEDROCK_RUNTIME`` / ``AWS_ENDPOINT_URL_BEDROCK`` at call time) or
that the capsule recorded a Bedrock call to is answered from the capsule, one
recorded call per request, in recorded order; nothing reaches the network.
botocore then parses the served bytes with its own parser (and raises its own
``ClientError`` for a served HTTP error), so the workload receives what botocore
builds from a Bedrock response. Every other request passes through unchanged.

Why ``urllib3`` and not botocore's ``before-send`` event: the event is registered
per client (or per session, before the client exists), so a client created before
the dispatcher, or by a library with its own session, would go live; the
``urlopen`` patch covers every botocore client and is the same layer capture
records at. The SigV4 ``Authorization`` / ``X-Amz-Date`` headers are never read:
serving is positional, so a request signed with other credentials or at another
time is served the same.

This module holds the pure part: request -> queue, and record -> status, headers
and body bytes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from novafabric.capture.hooks._aws_eventstream import encode_message
from novafabric.capture.hooks._bedrock import (
    BEDROCK_BLOCKS_EXT,
    BEDROCK_CONVERSE_STREAM_SURFACE,
    BEDROCK_CONVERSE_SURFACE,
    BEDROCK_EVENTS_EXT,
    BEDROCK_FINAL_EXT,
    BEDROCK_HEADERS_EXT,
    BEDROCK_HTTP_ERROR_EXT,
    BEDROCK_SYSTEM,
    bedrock_surface,
)
from novafabric.capture.hooks._finish_reason import PROVIDER_FINISH_REASONS_EXT

#: The method mocked replay patches to serve Bedrock calls: (module, class, attribute).
SERVED_URLLIB3_SURFACE: tuple[str, str, str] = (
    "urllib3.connectionpool", "HTTPConnectionPool", "urlopen",
)
BEDROCK_QUEUES: tuple[str, ...] = (BEDROCK_CONVERSE_SURFACE, BEDROCK_CONVERSE_STREAM_SURFACE)

_JSON = "application/json"
_EVENT_STREAM = "application/vnd.amazon.eventstream"
_EVENT_HEADERS = ((":content-type", _JSON), (":message-type", "event"))
#: Canonical finish reason -> a Bedrock ``stopReason`` (when the raw one is missing).
_CANONICAL_TO_STOP = {
    "stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens",
    "content_filter": "content_filtered",
}
_REASONS = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
            408: "Request Timeout", 424: "Failed Dependency", 429: "Too Many Requests",
            500: "Internal Server Error", 503: "Service Unavailable"}


class BedrockServeError(ValueError):
    """A recorded Bedrock record that cannot be served back faithfully."""


@dataclass(frozen=True)
class ServedHttp:
    """What the patched ``urlopen`` returns: status, headers in order, body."""

    status: int
    reason: str
    headers: list[tuple[str, str]]
    body: bytes


def _netloc(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        return f"{host}:{parsed.port}" if parsed.port else host
    except ValueError:
        return ""


def recorded_bedrock_hosts(records: list[dict[str, Any]]) -> frozenset[str]:
    """``host[:port]`` of every Bedrock endpoint the capsule recorded a call to."""
    return frozenset(
        _netloc(r["endpoint"]) for r in records
        if r.get("gen_ai.system") == BEDROCK_SYSTEM and isinstance(r.get("endpoint"), str)
        and _netloc(r["endpoint"])
    )


def request_queue(
    method: str, url: str, gen_ai_system: str, recorded_hosts: frozenset[str]
) -> str | None:
    """The Bedrock queue a request is served from, or ``None`` (pass through)."""
    if method.upper() != "POST":
        return None
    system = gen_ai_system
    if system != BEDROCK_SYSTEM and _netloc(url) in recorded_hosts:
        system = BEDROCK_SYSTEM
    return bedrock_surface(url, system)


def request_json(body: Any) -> dict[str, Any]:
    """The JSON request body, when it is bytes or text (an iterator is never read)."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    if not isinstance(body, (bytes, bytearray, memoryview)):
        return {}
    try:
        parsed = json.loads(bytes(body) or b"{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _ext(record: dict[str, Any]) -> dict[str, Any]:
    ext = record.get("extensions")
    return ext if isinstance(ext, dict) else {}


def _dumps(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _text(message: dict[str, Any]) -> str:
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _message(record: dict[str, Any]) -> dict[str, Any]:
    choices = record.get("gen_ai.response.choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    message = choice.get("message") if isinstance(choice, dict) else None
    return message if isinstance(message, dict) else {}


def _stop_reason(record: dict[str, Any]) -> str | None:
    raw = _ext(record).get(PROVIDER_FINISH_REASONS_EXT)
    if isinstance(raw, list) and raw and isinstance(raw[0], str):
        return raw[0]
    choices = record.get("gen_ai.response.choices") or [{}]
    finish = choices[0].get("finish_reason") if isinstance(choices[0], dict) else None
    return _CANONICAL_TO_STOP.get(str(finish)) if finish else None


def _calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    calls = message.get("tool_calls")
    return [c for c in calls if isinstance(c, dict)] if isinstance(calls, list) else []


def _layout(record: dict[str, Any], message: dict[str, Any]) -> list[dict[str, Any]]:
    """The recorded block layout, checked against the canonical message."""
    text = _text(message)
    calls = _calls(message)
    layout = _ext(record).get(BEDROCK_BLOCKS_EXT)
    if layout is None:
        out: list[dict[str, Any]] = [{"type": "text", "chars": len(text)}] if text else []
        return out + [{"type": "toolUse", "call": k} for k in range(len(calls))]
    if not isinstance(layout, list) or not all(isinstance(e, dict) for e in layout):
        raise BedrockServeError("the recorded content-block layout is malformed")
    chars = sum(e.get("chars", 0) for e in layout if e.get("type") == "text")
    used = sorted(e.get("call") for e in layout if e.get("type") == "toolUse")
    if chars != len(text) or used != list(range(len(calls))):
        raise BedrockServeError(
            "the recorded content-block layout does not add up to the recorded message"
        )
    return layout


def _content_blocks(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``output.message.content`` blocks of a recorded non-streaming call."""
    message = _message(record)
    text = _text(message)
    calls = _calls(message)
    blocks: list[dict[str, Any]] = []
    at = 0
    for entry in _layout(record, message):
        kind = entry.get("type")
        if kind == "text":
            n = int(entry.get("chars", 0))
            blocks.append({"text": text[at:at + n]})
            at += n
        elif kind == "toolUse":
            call = calls[int(entry["call"])]
            blocks.append({"toolUse": {"toolUseId": call.get("id", ""), "name": call["name"],
                                       "input": call.get("arguments", {})}})
        elif isinstance(entry.get("block"), dict):
            blocks.append(entry["block"])
        else:
            raise BedrockServeError("a recorded content block was too large to keep")
    return blocks


def _converse_body(record: dict[str, Any]) -> bytes:
    final = _ext(record).get(BEDROCK_FINAL_EXT)
    blocks = _content_blocks(record)
    message = _message(record)
    if isinstance(final, dict):
        body = json.loads(json.dumps(final))
        output = body.get("output")
        if not (isinstance(output, dict) and isinstance(output.get("message"), dict)):
            raise BedrockServeError("the recorded response body has no output message")
        output["message"]["content"] = blocks
        return _dumps(body)
    body = {
        "output": {"message": {"role": message.get("role", "assistant"), "content": blocks}},
        "stopReason": _stop_reason(record),
        "usage": _usage(record),
        "metrics": {"latencyMs": 0},
    }
    return _dumps(body)


def _usage(record: dict[str, Any]) -> dict[str, int]:
    inp = record.get("gen_ai.usage.input_tokens") or 0
    out = record.get("gen_ai.usage.output_tokens") or 0
    return {"inputTokens": int(inp), "outputTokens": int(out), "totalTokens": int(inp) + int(out)}


def _event(event_type: str, payload: Any) -> bytes:
    return encode_message([(":event-type", event_type), *_EVENT_HEADERS], _dumps(payload))


def _stream_body(record: dict[str, Any]) -> bytes:
    """The recorded ``ConverseStream`` events, re-encoded as an AWS event stream."""
    message = _message(record)
    text = _text(message)
    calls = _calls(message)
    layout = _layout(record, message)
    # The text / raw tool input of each content block, by contentBlockIndex.
    pieces: dict[int, str] = {}
    at = 0
    for position, entry in enumerate(layout):
        index = entry.get("index", position)
        if entry.get("type") == "text":
            n = int(entry.get("chars", 0))
            pieces[index] = text[at:at + n]
            at += n
        elif entry.get("type") == "toolUse":
            raw = entry.get("input")
            pieces[index] = raw if isinstance(raw, str) else json.dumps(
                calls[int(entry["call"])].get("arguments", {})
            )
    events = _ext(record).get(BEDROCK_EVENTS_EXT)
    if not isinstance(events, list):
        return b"".join(_standard_events(record, layout, pieces, calls))
    cursor: dict[int, int] = {}
    frames: list[bytes] = []
    for entry in events:
        frames.append(_frame(entry, pieces, cursor))
    for index, piece in pieces.items():
        if cursor.get(index, 0) != len(piece):
            raise BedrockServeError("the recorded stream events do not add up to the response")
    return b"".join(frames)


def _frame(entry: Any, pieces: dict[int, str], cursor: dict[int, int]) -> bytes:
    if not (isinstance(entry, list) and entry and isinstance(entry[0], str)):
        raise BedrockServeError("a recorded stream event is malformed")
    kind = entry[0]
    if kind == "delta" and len(entry) == 4:
        _, index, delta_kind, n = entry
        piece = pieces.get(index)
        if piece is None or not isinstance(n, int):
            raise BedrockServeError("a recorded stream delta names no content block")
        start = cursor.get(index, 0)
        part = piece[start:start + n]
        if len(part) != n:
            raise BedrockServeError("a recorded stream delta runs past its content block")
        cursor[index] = start + n
        delta = {"text": part} if delta_kind == "text" else {"toolUse": {"input": part}}
        return _event("contentBlockDelta", {"contentBlockIndex": index, "delta": delta})
    if kind in ("exception", "raw") and len(entry) == 3 and isinstance(entry[1], dict):
        headers = [(str(k), str(v)) for k, v in entry[1].items()]
        return encode_message(headers, str(entry[2]).encode("utf-8"))
    if len(entry) == 2 and isinstance(entry[1], dict):
        return _event(kind, entry[1])
    raise BedrockServeError(f"a recorded stream event of kind {kind!r} is malformed")


def _standard_events(
    record: dict[str, Any], layout: list[dict[str, Any]], pieces: dict[int, str],
    calls: list[dict[str, Any]],
) -> list[bytes]:
    """A standard event sequence when the capture kept no event map (too many events)."""
    message = _message(record)
    frames = [_event("messageStart", {"role": message.get("role", "assistant")})]
    for position, entry in enumerate(layout):
        index = entry.get("index", position)
        if entry.get("type") == "toolUse":
            call = calls[int(entry["call"])]
            frames.append(_event("contentBlockStart", {"contentBlockIndex": index, "start": {
                "toolUse": {"toolUseId": call.get("id", ""), "name": call["name"]}}}))
            delta: dict[str, Any] = {"toolUse": {"input": pieces[index]}}
        elif entry.get("type") == "text":
            delta = {"text": pieces[index]}
        else:
            raise BedrockServeError("a recorded content block cannot be re-streamed")
        frames.append(_event("contentBlockDelta", {"contentBlockIndex": index, "delta": delta}))
        frames.append(_event("contentBlockStop", {"contentBlockIndex": index}))
    stop = _stop_reason(record)
    if stop is not None:
        frames.append(_event("messageStop", {"stopReason": stop}))
        frames.append(_event("metadata", {"usage": _usage(record), "metrics": {"latencyMs": 0}}))
    return frames


def _headers(record: dict[str, Any], body: bytes, content_type: str) -> list[tuple[str, str]]:
    recorded = _ext(record).get(BEDROCK_HEADERS_EXT)
    out: list[tuple[str, str]] = []
    seen_length = False
    if isinstance(recorded, list):
        for pair in recorded:
            if not (isinstance(pair, list) and len(pair) == 2):
                continue
            name, value = str(pair[0]), str(pair[1])
            if name.lower() == "content-length":
                value, seen_length = str(len(body)), True
            out.append((name, value))
    if not any(n.lower() == "content-type" for n, _ in out):
        out.insert(0, ("content-type", content_type))
    if not seen_length:
        out.append(("content-length", str(len(body))))
    return out


def unservable_reason(record: dict[str, Any]) -> str | None:
    """Why a queued Bedrock record cannot be served, or ``None`` (checked before
    the record is consumed, like a malformed tool-call reference)."""
    try:
        served_response(_queue_of(record), record)
    except BedrockServeError as why:
        return str(why)
    return None


def _queue_of(record: dict[str, Any]) -> str:
    surface = _ext(record).get("io.novafabric.api_surface")
    return str(surface) if surface in BEDROCK_QUEUES else BEDROCK_CONVERSE_SURFACE


def served_response(queue: str, record: dict[str, Any]) -> ServedHttp:
    """The HTTP response a recorded Bedrock call is served as.

    A recorded HTTP error (``io.novafabric.bedrock.http_error``) is served with its
    status and body, so botocore raises (or, for a throttle, retries) exactly as at
    capture. Raises :class:`BedrockServeError` when the record does not hold a
    response that can be served faithfully."""
    error = _ext(record).get(BEDROCK_HTTP_ERROR_EXT)
    if record.get("status", "success") != "success" and isinstance(error, dict):
        status, text = error.get("status"), error.get("body")
        if not (isinstance(status, int) and 300 <= status <= 599 and isinstance(text, str)):
            raise BedrockServeError("the recorded HTTP error is malformed")
        body = text.encode("utf-8")
        return ServedHttp(status, _REASONS.get(status, "Error"),
                          _headers(record, body, _JSON), body)
    if queue == BEDROCK_CONVERSE_STREAM_SURFACE:
        body = _stream_body(record)
        return ServedHttp(200, "OK", _headers(record, body, _EVENT_STREAM), body)
    if record.get("status", "success") != "success":
        raise BedrockServeError("the recorded call failed without a recorded response")
    body = _converse_body(record)
    return ServedHttp(200, "OK", _headers(record, body, _JSON), body)
