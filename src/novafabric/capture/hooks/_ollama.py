"""Ollama ``/api/chat`` and ``/api/generate`` responses for the wire hook (ADR-0320).

The ``httpx`` hook classifies a request to a registry-matched Ollama host as a
model call, but recorded no response (``gen_ai.response.choices: []``), so mocked
replay had nothing to serve. This module maps an Ollama response -- one JSON
object (``"stream": false``) or NDJSON lines (the API's default, which the
``ollama`` SDK and ``langchain_ollama`` both use: the SDK when ``stream=True``,
``langchain_ollama`` always) -- onto the canonical model-call record:

* ``gen_ai.response.choices[0].message``: ``role``, the concatenated ``content``
  (``/api/chat``) or ``response`` (``/api/generate``) text, and
  ``message.tool_calls`` as canonical ``{id, name, arguments}`` refs (``id`` is
  the provider's when it sent one, else ``""``; never invented);
* ``finish_reason`` from ``done``/``done_reason``: ``tool_calls`` when the turn
  holds tool calls, ``length`` for ``done_reason: length``, ``stop`` otherwise;
  ``null`` when no ``done: true`` object was delivered (an abandoned stream);
* usage from ``prompt_eval_count`` / ``eval_count``; the response ``model``;
* ``extensions["io.novafabric.api_surface"]``: ``ollama.chat`` | ``ollama.generate``;
* ``extensions["io.novafabric.ollama.final"]``: the scalar fields of the final
  object verbatim (``done_reason``, ``created_at``, durations, counts), so replay
  serves them back as recorded; ``extensions["io.novafabric.ollama.thinking"]``:
  the concatenated ``thinking`` text, when the model sent any.

Pure functions and small classes; no I/O. The hook decides when (buffered body:
at ``send``; streamed body: when the workload's read ends) -- see ``_httpx``.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from novafabric.capture.hooks._finish_reason import attach_provider_finish_reasons
from novafabric.capture.hooks._sdk_streams import API_SURFACE_EXT, attach_stream_info
from novafabric.capture.hooks._tool_call_refs import (
    note_dropped_tool_calls,
    parse_tool_arguments,
)

OLLAMA_CHAT_SURFACE = "ollama.chat"
OLLAMA_GENERATE_SURFACE = "ollama.generate"

#: URL path suffix -> ``API_SURFACE_EXT`` value.
_PATH_SURFACE: dict[str, str] = {
    "/api/chat": OLLAMA_CHAT_SURFACE,
    "/api/generate": OLLAMA_GENERATE_SURFACE,
}

#: Reverse-DNS extension key: the scalar fields of the final (``done``) object.
OLLAMA_FINAL_EXT = "io.novafabric.ollama.final"
#: Reverse-DNS extension key: the concatenated ``thinking`` text.
OLLAMA_THINKING_EXT = "io.novafabric.ollama.thinking"
#: Reverse-DNS extension key on a streamed record: the shape of every delivered
#: object, ``[content_chars, thinking_chars, tool_calls]`` in order, so replay
#: serves the recorded text back in the same chunks (a workload that stops
#: reading after its first line sees the same first line). Absent past
#: :data:`MAX_CHUNK_MAP` objects (replay then serves the text in one chunk).
OLLAMA_CHUNKS_EXT = "io.novafabric.ollama.chunks"
MAX_CHUNK_MAP = 10_000
#: Reverse-DNS extension key: why a response body was not recorded.
RESPONSE_NOT_RECORDED_EXT = "io.novafabric.response_not_recorded"

#: Fields of the final object that are not copied into ``OLLAMA_FINAL_EXT``:
#: carried elsewhere in the record (model, message, text, thinking), implied by
#: the record (``done``), or unbounded / not served (``context``, ``logprobs``).
_FINAL_EXCLUDED = frozenset(
    {"model", "message", "response", "thinking", "done", "context", "logprobs", "error"}
)
_MAX_FINAL_FIELDS = 32
_MAX_FINAL_STR = 1024

#: A single NDJSON line longer than this is not parsed (the body is then not
#: recorded); bounds the splitter's buffer on a body that is not NDJSON at all.
MAX_LINE_BYTES = 64 * 1024 * 1024

#: ``options`` keys mapped onto the semconv request attributes.
_OPTION_KEYS: tuple[tuple[str, str], ...] = (
    ("temperature", "temperature"),
    ("top_p", "top_p"),
    ("top_k", "top_k"),
    ("seed", "seed"),
    ("num_predict", "max_tokens"),
    ("stop", "stop"),
    ("frequency_penalty", "frequency_penalty"),
    ("presence_penalty", "presence_penalty"),
)


def ollama_surface(url: str, gen_ai_system: str) -> str | None:
    """``ollama.chat`` / ``ollama.generate`` for a request to an Ollama host, else None."""
    if gen_ai_system != "ollama":
        return None
    path = urlparse(url).path.rstrip("/")
    for suffix, surface in _PATH_SURFACE.items():
        if path.endswith(suffix):
            return surface
    return None


def request_streams(body: dict[str, Any]) -> bool:
    """Ollama streams unless the request says ``"stream": false``."""
    return body.get("stream") is not False


def request_body(body: dict[str, Any], surface: str) -> dict[str, Any]:
    """An OpenAI-shaped copy of an Ollama request body for
    :func:`~novafabric.capture.hooks._otel_genai.extract_request_attributes`.

    ``options`` sampling keys become top-level keys (``num_predict`` ->
    ``max_tokens``) unless already present; an ``/api/generate`` request's
    ``system`` and ``prompt`` become ``system``/``user`` messages.
    """
    out = dict(body)
    options = body.get("options")
    if isinstance(options, dict):
        for src, dst in _OPTION_KEYS:
            if src in options and dst not in out:
                out[dst] = options[src]
    if surface == OLLAMA_GENERATE_SURFACE and "messages" not in out:
        messages: list[dict[str, Any]] = []
        if isinstance(body.get("system"), str) and body["system"]:
            messages.append({"role": "system", "content": body["system"]})
        if isinstance(body.get("prompt"), str):
            messages.append({"role": "user", "content": body["prompt"]})
        out["messages"] = messages
    return out


def operation_name(surface: str) -> str:
    return "text_completion" if surface == OLLAMA_GENERATE_SURFACE else "chat"


class NdjsonSplitter:
    """Split a byte stream into parsed JSON objects, one per line.

    A body that is one JSON object without a trailing newline is the same as
    one line. A line that does not parse, or a line over :data:`MAX_LINE_BYTES`,
    sets :attr:`failed` and stops parsing (nothing more is returned).
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.failed: str | None = None

    def feed(self, data: bytes) -> list[Any]:
        if self.failed is not None or not data:
            return []
        self._buffer.extend(data)
        out: list[Any] = []
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            if not self._parse(line, out):
                return out
        if len(self._buffer) > MAX_LINE_BYTES:
            self._fail(f"an NDJSON line exceeded {MAX_LINE_BYTES} bytes")
        return out

    def flush(self) -> list[Any]:
        if self.failed is not None:
            return []
        out: list[Any] = []
        line = bytes(self._buffer)
        self._buffer.clear()
        self._parse(line, out)
        return out

    def _parse(self, line: bytes, out: list[Any]) -> bool:
        if not line.strip():
            return True
        try:
            out.append(json.loads(line))
        except ValueError:
            self._fail("the response body is not JSON / NDJSON")
            return False
        return True

    def _fail(self, reason: str) -> None:
        self.failed = reason
        self._buffer.clear()


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


class OllamaAccumulator:
    """Fold Ollama response objects (one, or NDJSON chunks) into one response."""

    def __init__(self, surface: str) -> None:
        self.surface = surface
        self.model: str | None = None
        self.role = "assistant"
        self.content: list[str] = []
        self.thinking: list[str] = []
        self.tool_calls: list[Any] = []
        self.final: dict[str, Any] | None = None
        self.error: str | None = None
        #: Response objects delivered (error lines not included).
        self.objects = 0
        #: ``OLLAMA_CHUNKS_EXT`` entries; ``None`` once past ``MAX_CHUNK_MAP``.
        self.chunks: list[list[int]] | None = []

    @property
    def done(self) -> bool:
        return self.final is not None

    def add(self, obj: Any) -> None:
        if not isinstance(obj, dict):
            return
        if isinstance(obj.get("error"), str):
            # An in-stream error line: the SDK raises on it (ollama ResponseError).
            if self.error is None:
                self.error = obj["error"]
            return
        self.objects += 1
        if isinstance(obj.get("model"), str) and obj["model"]:
            self.model = self.model or obj["model"]
        text = thinking = ""
        named = 0
        if self.surface == OLLAMA_CHAT_SURFACE:
            message = obj.get("message")
            if isinstance(message, dict):
                if isinstance(message.get("role"), str) and message["role"]:
                    self.role = message["role"]
                text, thinking = _str(message.get("content")), _str(message.get("thinking"))
                calls = message.get("tool_calls")
                if isinstance(calls, list):
                    self.tool_calls.extend(calls)
                    named = sum(1 for c in calls if _call_name(c) is not None)
        else:
            text, thinking = _str(obj.get("response")), _str(obj.get("thinking"))
        self.content.append(text)
        self.thinking.append(thinking)
        if self.chunks is not None:
            if len(self.chunks) >= MAX_CHUNK_MAP:
                self.chunks = None
            else:
                self.chunks.append([len(text), len(thinking), named])
        if obj.get("done") is True:
            self.final = obj

    def response(self) -> OllamaAccumulator:
        """For :class:`~novafabric.capture.hooks._sdk_streams._StreamState`."""
        return self


def _call_name(call: Any) -> str | None:
    function = call.get("function") if isinstance(call, dict) else None
    name = function.get("name") if isinstance(function, dict) else None
    return name if isinstance(name, str) and name else None


def _tool_call_refs(calls: list[Any]) -> tuple[list[dict[str, Any]], int]:
    refs: list[dict[str, Any]] = []
    dropped = 0
    for call in calls:
        name = _call_name(call)
        if name is None:
            dropped += 1
            continue
        function = call["function"]
        refs.append({
            "id": str(call.get("id") or ""),
            "name": name,
            "arguments": parse_tool_arguments(function.get("arguments")),
        })
    return refs, dropped


def _final_fields(final: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in final.items():
        if key in _FINAL_EXCLUDED or len(out) >= _MAX_FINAL_FIELDS:
            continue
        if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
            out[key] = value
        elif isinstance(value, str) and len(value) <= _MAX_FINAL_STR:
            out[key] = value
    return out


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def apply_response(
    record: dict[str, Any],
    acc: OllamaAccumulator,
    *,
    streamed: bool,
    chunk_count: int,
    first_ms: int | None,
    error: BaseException | None = None,
    not_recorded: str | None = None,
    complete: bool | None = None,
) -> None:
    """Write the response side of an Ollama record (in place).

    ``complete`` (streamed bodies): whether the workload read the body to its end
    and it held the ``done`` object; ``None`` means "the done object arrived".
    The record holds every object the transport delivered, so a stream the
    workload closed early may still carry the ``done`` object -- it is flagged
    ``io.novafabric.stream_complete: false`` all the same. Never raises for a
    well-typed accumulator; the caller wraps it in ``fail_open``."""
    extensions = record.setdefault("extensions", {})
    extensions[API_SURFACE_EXT] = acc.surface
    if not_recorded is not None:
        extensions[RESPONSE_NOT_RECORDED_EXT] = not_recorded
        return
    if acc.model:
        record["gen_ai.response.model"] = acc.model
    refs, dropped = _tool_call_refs(acc.tool_calls)
    final = acc.final
    raw_reason = final.get("done_reason") if final is not None else None
    raw_reason = raw_reason if isinstance(raw_reason, str) and raw_reason else None
    finish: str | None
    if final is None:
        finish = None
    elif refs:
        finish = "tool_calls"
    elif raw_reason == "length":
        finish = "length"
    else:
        finish = "stop"
    if acc.objects:
        message: dict[str, Any] = {"role": acc.role, "content": "".join(acc.content)}
        if refs:
            message["tool_calls"] = refs
        record["gen_ai.response.choices"] = [
            {"index": 0, "message": message, "finish_reason": finish}
        ]
    if finish is not None:
        record["gen_ai.response.finish_reasons"] = [finish]
        if raw_reason is not None:
            attach_provider_finish_reasons(record, [raw_reason], [finish])
    if final is not None:
        record["gen_ai.usage.input_tokens"] = _count(final.get("prompt_eval_count"))
        record["gen_ai.usage.output_tokens"] = _count(final.get("eval_count"))
        extensions[OLLAMA_FINAL_EXT] = _final_fields(final)
    thinking = "".join(acc.thinking)
    if thinking:
        extensions[OLLAMA_THINKING_EXT] = thinking
    note_dropped_tool_calls(record, dropped)
    if streamed:
        done = final is not None if complete is None else complete
        attach_stream_info(record, (chunk_count, first_ms, done))
        if acc.chunks:
            extensions[OLLAMA_CHUNKS_EXT] = acc.chunks
    if acc.error is not None:
        record["status"] = "error"
        record["error"] = {
            "type": "OllamaResponseError", "message": acc.error, "traceback_ref": None,
        }
    elif error is not None:
        record["status"] = "error"
        record["error"] = {
            "type": type(error).__name__, "message": str(error), "traceback_ref": None,
        }
