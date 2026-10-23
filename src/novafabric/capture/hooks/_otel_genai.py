"""OTel GenAI semconv attribute extraction (RFC-0001 Option C, sub-track C-4).

A single source of truth for "given a parsed JSON request body and the
URL it was sent to, what `gen_ai.*` attributes can we populate?"

Wire-level hooks (`_httpx`, `_requests`, `_aiohttp`, `_urllib3`) call
:func:`extract_request_attributes` instead of hand-rolling field extraction.
This guarantees:

1. **Consistency.** All hooks emit the same field set for a given body
   shape — no drift between sync httpx and async aiohttp.
2. **Coverage.** Every semconv "required when applicable" field
   (temperature, max_tokens, top_p, top_k, stop_sequences, seed,
   frequency_penalty, presence_penalty) is extracted when present.
3. **Forward extensibility.** Adding a new semconv field means editing
   one file, not four.

Per the private `design/spec/model-call-v0.md`, fields are emitted only when present
in the body — there is no "default 0" for unset fields. This matches
the OTel semconv contract and avoids polluting records with synthetic
values.

Body shapes supported today (all OpenAI-compatible — Anthropic
Messages API, OpenAI Chat Completions, Mistral, Together, Cohere via
OpenAI-compatible endpoint, etc.). Per-provider body adapters for
non-OpenAI shapes (Bedrock-Cohere, Bedrock-Titan, Bedrock-Llama,
native Vertex, native watsonx) are queued as v0.6 sub-track C-3.3b.
"""
from __future__ import annotations

from typing import Any

from novafabric.capture._jsonable import to_jsonable

# OTel-aligned mapping from request-body keys to semconv attribute names.
# Both the OpenAI and Anthropic Messages API surface these keys with the
# same JSON name (the differences are at the SDK call site, not the
# wire); the Bedrock providers diverge and are handled separately
# in the planned C-3.3b body adapters.
_REQUEST_FIELD_MAP: tuple[tuple[str, str, type | tuple[type, ...]], ...] = (
    # (body-key, semconv-attribute, expected-type)
    ("temperature",       "gen_ai.request.temperature",       (int, float)),
    ("top_p",             "gen_ai.request.top_p",             (int, float)),
    ("top_k",             "gen_ai.request.top_k",             int),
    ("max_tokens",        "gen_ai.request.max_tokens",        int),
    ("frequency_penalty", "gen_ai.request.frequency_penalty", (int, float)),
    ("presence_penalty",  "gen_ai.request.presence_penalty",  (int, float)),
    ("seed",              "gen_ai.request.seed",              int),
)


def _extract_stop_sequences(body: dict[str, Any]) -> list[str] | None:
    """Normalize OpenAI's ``stop`` (str | list) and Anthropic's
    ``stop_sequences`` (list) into a single ``list[str]``."""
    if "stop_sequences" in body:
        value = body["stop_sequences"]
        if isinstance(value, list) and all(isinstance(s, str) for s in value):
            return list(value)
    if "stop" in body:
        value = body["stop"]
        if isinstance(value, str):
            return [value]
        if isinstance(value, list) and all(isinstance(s, str) for s in value):
            return list(value)
    return None


def _extract_choice_count(body: dict[str, Any]) -> int | None:
    """OTel: ``gen_ai.request.choice.count``. OpenAI uses ``n``; Anthropic
    does not support multiple choices."""
    n = body.get("n")
    if isinstance(n, int) and n > 0:
        return n
    return None


def _request_messages(value: Any) -> list[Any]:
    """``messages`` as a JSON-safe list.

    The SDK hooks pass the caller's own ``kwargs``, whose messages may hold SDK
    objects (``completion.choices[0].message`` passed back into the next call).
    Each one is converted the way the SDK serialises it on the wire (see
    :mod:`novafabric.capture._jsonable`), so the record never holds a value
    ``json.dumps`` rejects and later steps (media annotation) see plain dicts.
    Wire-hook bodies are parsed JSON already and come back equal.
    """
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        try:
            value = list(value)
        except TypeError:  # not iterable: nothing to record
            return []
    return [to_jsonable(m) for m in value]


def extract_request_attributes(
    body: dict[str, Any],
    *,
    url: str = "",
    gen_ai_system: str = "unknown",
    operation_name: str = "chat",
) -> dict[str, Any]:
    """Extract all semconv-defined ``gen_ai.*`` attributes present in
    an OpenAI-shaped request body.

    Returns a dict ready to merge into a ``model-calls.jsonl`` record.
    Fields absent from the body are also absent from the returned dict
    (no ``None`` placeholders; no zero defaults).

    Always-included keys (with sensible defaults if missing):
      - ``gen_ai.system``
      - ``gen_ai.operation.name``
      - ``gen_ai.request.model``
      - ``gen_ai.response.model``  (mirrors request.model — wire-level
        hooks cannot know what the response actually said until parsing)
      - ``gen_ai.request.messages``  (defaults to ``[]``)
      - ``gen_ai.response.choices``  (defaults to ``[]``)
      - ``gen_ai.usage.input_tokens``  (defaults to 0)
      - ``gen_ai.usage.output_tokens``  (defaults to 0)
      - ``endpoint``  (the request URL)
    """
    if not isinstance(body, dict):
        body = {}

    # C-3.3b: normalize non-OpenAI body shapes (Bedrock providers, etc.)
    # before extraction. Adapters are pure dict→dict transforms with no
    # side effects; passthrough for OpenAI-shaped bodies.
    from novafabric.capture.body_adapters import adapt_body
    body = adapt_body(body, url=url)

    model = str(body.get("model", "unknown"))
    attrs: dict[str, Any] = {
        "gen_ai.system": gen_ai_system,
        "gen_ai.operation.name": operation_name,
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
        "gen_ai.request.messages": _request_messages(body.get("messages")),
        "gen_ai.response.choices": [],
        "gen_ai.usage.input_tokens": 0,
        "gen_ai.usage.output_tokens": 0,
        "endpoint": url,
    }

    # Optional/conditional fields per OTel semconv "Required when applicable".
    for body_key, attr_name, expected_type in _REQUEST_FIELD_MAP:
        if body_key in body:
            value = body[body_key]
            if isinstance(value, expected_type):
                attrs[attr_name] = value

    stops = _extract_stop_sequences(body)
    if stops is not None:
        attrs["gen_ai.request.stop_sequences"] = stops

    n = _extract_choice_count(body)
    if n is not None:
        attrs["gen_ai.request.choice.count"] = n

    return attrs


def build_record_envelope(
    *,
    model_call_id: str,
    parent_span_id: str,
    started_at: str,
    finished_at: str,
    duration_ms: int,
    status: str,
) -> dict[str, Any]:
    """Build the ``schema_version`` / timing envelope every record needs.

    Returned dict is meant to be merged with the result of
    :func:`extract_request_attributes`.
    """
    return {
        "schema_version": "0.1.0",
        "semconv_version": "1.30.0",
        "model_call_id": model_call_id,
        "parent_span_id": parent_span_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "status": status,
    }


#: Instance attributes in which a client library keeps a body it has already
#: read: ``requests``/``httpx``/``httpx2`` use ``_content``, ``aiohttp`` and
#: ``urllib3`` use ``_body``. Read from the instance ``__dict__`` only, so no
#: property or descriptor -- nothing that could do I/O -- ever runs.
_BUFFERED_BODY_ATTRS = ("_content", "_body")


def _buffered_body_size(response: Any) -> int | None:
    try:
        attrs = vars(response)
    except TypeError:  # no __dict__ (slots): nothing we may look at
        return None
    for name in _BUFFERED_BODY_ATTRS:
        value = attrs.get(name)
        if isinstance(value, (bytes, bytearray)):
            return len(value)
    return None


def _declared_content_length(response: Any) -> int | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get("content-length") or headers.get("Content-Length")
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return int(value) if value.isdigit() else None


def safe_response_size(response: Any) -> int | None:
    """The response body size in bytes, **without ever touching the body**.

    A wire hook runs inside the user's ``send``; a streamed response
    (``stream=True``, ``client.stream()``, an SSE feed) is handed back before
    its body is read, and the hook must neither consume, buffer nor wait for
    that body (ADR-0021: capture never changes the workload's behaviour).
    ``requests.Response.content`` *reads the whole body*: calling it here made
    ``Session.send`` block until a streamed response ended -- forever, for an
    endless SSE stream. So this never calls ``.content``/``.data``/``read()``:

    1. a body the library has **already buffered** (a non-streamed response)
       -> its exact length;
    2. otherwise the declared ``Content-Length`` header -> that value (the
       size on the wire, i.e. before any content-decoding);
    3. otherwise -> ``None`` (unknown): a chunked or endless stream.

    Never raises.
    """
    if response is None:
        return None
    try:
        size = _buffered_body_size(response)
        return size if size is not None else _declared_content_length(response)
    except Exception:  # noqa: BLE001 -- sizing is best-effort, never fatal
        return None
