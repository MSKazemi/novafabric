"""Serving recorded Ollama calls at the ``httpx`` transport (ADR-0320, experimental).

Mocked replay patches ``HTTPTransport.handle_request`` and
``AsyncHTTPTransport.handle_async_request`` (``httpx``, and ``httpx2`` when it is
importable). A request to a registry-matched Ollama host -- the URL registry,
including ``OLLAMA_BASE_URL`` / ``OLLAMA_HOST`` read at call time, or a host the
capsule recorded an Ollama call to -- whose path is ``/api/chat`` or
``/api/generate`` is answered from the capsule, one recorded call per request in
recorded order (``MockModelDispatcher`` queues ``ollama.chat`` /
``ollama.generate``); nothing reaches the network. Every other request passes to
the real transport unchanged.

This module holds the pure part: the request a transport sees -> its queue, and a
recorded record -> the Ollama response objects (one JSON object, or NDJSON chunks
ending with the ``done`` object) that the served body carries.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from novafabric.capture.hooks._ollama import (
    OLLAMA_CHAT_SURFACE,
    OLLAMA_CHUNKS_EXT,
    OLLAMA_FINAL_EXT,
    OLLAMA_GENERATE_SURFACE,
    OLLAMA_THINKING_EXT,
    ollama_surface,
)

#: The transport methods mocked replay patches to serve Ollama calls:
#: (module, class, attribute, async).
SERVED_TRANSPORT_SURFACES: tuple[tuple[str, str, str, bool], ...] = (
    ("httpx", "HTTPTransport", "handle_request", False),
    ("httpx", "AsyncHTTPTransport", "handle_async_request", True),
    ("httpx2", "HTTPTransport", "handle_request", False),
    ("httpx2", "AsyncHTTPTransport", "handle_async_request", True),
)

#: Replay queue -> the path it serves.
OLLAMA_QUEUES: dict[str, str] = {
    OLLAMA_CHAT_SURFACE: "/api/chat",
    OLLAMA_GENERATE_SURFACE: "/api/generate",
}

#: ``created_at`` when the record kept none (never the case for an ADR-0320
#: record whose call finished; a partial stream may lack it).
_EPOCH = "1970-01-01T00:00:00Z"


def _netloc(url: str) -> str:
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        return f"{host}:{parsed.port}" if parsed.port else host
    except ValueError:
        return ""


def recorded_ollama_hosts(records: list[dict[str, Any]]) -> frozenset[str]:
    """``host[:port]`` of every Ollama endpoint the capsule recorded a call to."""
    hosts = set()
    for record in records:
        if record.get("gen_ai.system") == "ollama" and isinstance(record.get("endpoint"), str):
            netloc = _netloc(record["endpoint"])
            if netloc:
                hosts.add(netloc)
    return frozenset(hosts)


def request_queue(
    method: str, url: str, gen_ai_system: str, recorded_hosts: frozenset[str]
) -> str | None:
    """The Ollama queue a transport request is served from, or ``None`` (live)."""
    if method.upper() != "POST":
        return None
    system = gen_ai_system
    if system != "ollama" and _netloc(url) in recorded_hosts:
        system = "ollama"
    return ollama_surface(url, system)


def request_json(content: bytes) -> dict[str, Any]:
    try:
        body = json.loads(content or b"{}")
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _ext(record: dict[str, Any]) -> dict[str, Any]:
    ext = record.get("extensions")
    return ext if isinstance(ext, dict) else {}


def _tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for ref in message.get("tool_calls") or []:
        if not (isinstance(ref, dict) and isinstance(ref.get("name"), str) and ref["name"]):
            continue
        arguments = ref.get("arguments")
        call: dict[str, Any] = {}
        if ref.get("id"):
            call["id"] = str(ref["id"])
        call["function"] = {
            "name": ref["name"],
            "arguments": arguments if isinstance(arguments, dict) else {},
        }
        out.append(call)
    return out


def _chunk_map(value: Any, text: str, thinking: str, calls: int) -> list[list[int]] | None:
    """The recorded chunk shapes, when they add up to the recorded response."""
    if not isinstance(value, list) or not value:
        return None
    rows: list[list[int]] = []
    for row in value:
        if not (isinstance(row, list) and len(row) == 3 and all(
            isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in row
        )):
            return None
        rows.append(list(row))
    sums = [sum(r[i] for r in rows) for i in range(3)]
    return rows if sums == [len(text), len(thinking), calls] else None


def ollama_response_objects(
    queue: str, record: dict[str, Any], *, stream: bool
) -> list[dict[str, Any]]:
    """The Ollama response objects a recorded call is served as.

    Non-streaming: one object (``done: true`` with the recorded final fields).
    Streaming: the recorded chunks (``io.novafabric.ollama.chunks``: the same
    text, thinking and tool calls in each, in order), the last one the ``done``
    object; without a chunk map, one chunk with everything and then the ``done``
    object. A call recorded without ``done`` (the stream ended or was closed
    before Ollama sent it) is served without it -- what was delivered, no more.
    """
    choices = record.get("gen_ai.response.choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    choice = choice if isinstance(choice, dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    assert isinstance(message, dict)
    finished = isinstance(choice.get("finish_reason"), str) and bool(choice["finish_reason"])
    ext = _ext(record)
    final = ext.get(OLLAMA_FINAL_EXT)
    final = dict(final) if isinstance(final, dict) else {}
    raw_thinking = ext.get(OLLAMA_THINKING_EXT)
    thinking = raw_thinking if isinstance(raw_thinking, str) else ""
    content = message.get("content")
    text = content if isinstance(content, str) else ""
    calls = _tool_calls(message)
    model = str(record.get("gen_ai.response.model") or record.get("gen_ai.request.model") or "")
    created_at = final.get("created_at") if isinstance(final.get("created_at"), str) else _EPOCH
    head = {"model": model, "created_at": created_at}
    role = str(message.get("role") or "assistant")
    tail = {k: v for k, v in final.items() if k not in ("model", "created_at")}
    chat = queue == OLLAMA_CHAT_SURFACE

    def obj(part: str, think: str, part_calls: list[dict[str, Any]], done: bool) -> dict[str, Any]:
        out: dict[str, Any] = dict(head)
        if chat:
            msg: dict[str, Any] = {"role": role, "content": part}
            if think:
                msg["thinking"] = think
            if part_calls:
                msg["tool_calls"] = part_calls
            out["message"] = msg
        else:
            out["response"] = part
            if think:
                out["thinking"] = think
        out["done"] = done
        if done:
            out.update(tail)
        return out

    if not stream:
        return [obj(text, thinking, calls, finished)]
    rows = _chunk_map(ext.get(OLLAMA_CHUNKS_EXT), text, thinking, len(calls))
    if rows is None:
        objects: list[dict[str, Any]] = []
        if text or thinking or calls:
            objects.append(obj(text, thinking, calls, False))
        if finished:
            objects.append(obj("", "", [], True))
        return objects
    objects = []
    at = [0, 0, 0]
    for index, (n_text, n_think, n_calls) in enumerate(rows):
        objects.append(obj(
            text[at[0]:at[0] + n_text], thinking[at[1]:at[1] + n_think],
            calls[at[2]:at[2] + n_calls], finished and index == len(rows) - 1,
        ))
        at = [at[0] + n_text, at[1] + n_think, at[2] + n_calls]
    return objects


def encode_objects(objects: list[dict[str, Any]], *, stream: bool) -> list[bytes]:
    """Body chunks: one NDJSON line per object when streaming, else one JSON body."""
    if stream:
        return [json.dumps(o, separators=(",", ":")).encode() + b"\n" for o in objects]
    return [json.dumps(objects[0], separators=(",", ":")).encode()] if objects else [b"{}"]
