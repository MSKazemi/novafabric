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
        "gen_ai.request.messages": list(body.get("messages", [])),
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


def safe_response_size(response: Any) -> int | None:
    """Return the response body size in bytes, or ``None`` if unavailable.

    Reading ``response.content`` raises on a streaming/unread HTTP response
    (e.g. ``httpx.ResponseNotRead`` when the body was sent with ``stream=True``,
    which LLM chat clients like ``langchain_ollama.ChatOllama`` use). This helper
    isolates that access so a streaming response yields ``None`` for the size
    rather than aborting the surrounding NetworkEvent record. Never raises.
    """
    if response is None:
        return None
    try:
        content = getattr(response, "content", None)
        if content:
            return len(content)
    except Exception:
        return None
    return None
