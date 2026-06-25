"""Body adapter contract (RFC-0001 Option C, sub-track C-3.3b).

Adapters normalize non-OpenAI-shaped request bodies into the OpenAI
shape that :func:`novafabric.capture.hooks._otel_genai.extract_request_attributes`
expects (``model``, ``messages``, ``temperature``, ``max_tokens``,
``top_p``, ``top_k``, ``stop_sequences``, ``seed``, ...).

Why this module exists: AWS Bedrock's body shape differs per provider
(Anthropic, Cohere, Titan, Llama all use different JSON keys), and the
``urllib3`` capture hook's generic extractor was producing
``gen_ai.request.model: "unknown"`` for every Bedrock call. Adapters
close that gap without per-provider hooks.

Public API:

- :class:`AdapterProtocol` — the contract every adapter satisfies
- :func:`adapt_body` — dispatcher: URL → adapter → normalized body
- :func:`get_adapter`, :func:`known_adapter_names` — registry lookups
- :func:`register_adapter`, :func:`unregister_adapter` — runtime
  extension (plugin entry point ``novafabric.body_adapters``)
- :class:`UnknownAdapterError` — raised when a name is not registered

Per ADR-0021 §6 (cost/overhead): adapters run on the wire-level hot
path, so they must do pure-Python dict transforms — no I/O, no heavy
parsing, no network calls.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "AdapterProtocol",
    "UnknownAdapterError",
    "adapt_body",
    "get_adapter",
    "known_adapter_names",
    "register_adapter",
    "unregister_adapter",
]


@runtime_checkable
class AdapterProtocol(Protocol):
    """Contract every body adapter satisfies.

    Adapters are stateless: ``adapt(body, url)`` is a pure function
    of its inputs.
    """

    name: str
    """Stable identifier (e.g. ``"aws.bedrock.anthropic"``). Used for
    registry lookup and logging."""

    def adapt(self, body: dict[str, Any], url: str) -> dict[str, Any]:
        """Normalize ``body`` into an OpenAI-shaped dict.

        Returns a NEW dict. Must not raise — capture is a recorder,
        not a validator. On unparseable input, return ``{}``.
        """
        ...


class UnknownAdapterError(ValueError):
    """Raised when a requested adapter name is not registered."""


# ── Built-in Bedrock URL routing ─────────────────────────────────────────────

# Bedrock URL pattern: /model/<modelId>/invoke or /model/<modelId>/invoke-with-response-stream
# modelId starts with the provider prefix.
_BEDROCK_URL_RE = re.compile(
    r"bedrock-runtime\.[^/]+\.amazonaws\.com/model/([^/]+)/(?:invoke|converse)"
)
_BEDROCK_PROVIDER_TO_ADAPTER = (
    # Order matters: longer prefix first wins so
    # "amazon.nova-..." matches Titan-style but a future "amazon.foo" could be
    # routed differently if registered.
    ("anthropic.", "aws.bedrock.anthropic"),
    ("cohere.",    "aws.bedrock.cohere"),
    ("amazon.titan-", "aws.bedrock.titan"),
    ("amazon.nova-",  "aws.bedrock.titan"),
    ("amazon.",       "aws.bedrock.titan"),  # broad Amazon-shape fallback
    ("meta.llama",    "aws.bedrock.llama"),
)


def _model_id_from_bedrock_url(url: str) -> str | None:
    m = _BEDROCK_URL_RE.search(url)
    if not m:
        return None
    return urllib.parse.unquote(m.group(1))


def _resolve_adapter_name_for_url(url: str) -> str | None:
    """Return the adapter name that should handle ``url``, or None if
    no built-in adapter applies (caller falls through to passthrough)."""
    if not url:
        return None
    model_id = _model_id_from_bedrock_url(url)
    if model_id is None:
        return None
    for prefix, name in _BEDROCK_PROVIDER_TO_ADAPTER:
        if model_id.startswith(prefix):
            return name
    return None


# ── Built-in adapter implementations ─────────────────────────────────────────


def _safe_dict(body: Any) -> dict[str, Any]:
    return body if isinstance(body, dict) else {}


class _BedrockAnthropicAdapter:
    name = "aws.bedrock.anthropic"

    def adapt(self, body: dict[str, Any], url: str) -> dict[str, Any]:
        body = _safe_dict(body)
        out: dict[str, Any] = {}
        # Inject model id from URL (Bedrock-Anthropic body has no `model`).
        model_id = _model_id_from_bedrock_url(url) or "unknown"
        out["model"] = model_id
        # Pass through OpenAI-shaped fields directly (Bedrock-Anthropic
        # uses the same JSON keys as native Anthropic for these).
        for key in (
            "messages", "system",
            "max_tokens", "temperature", "top_p", "top_k",
            "stop_sequences",
        ):
            if key in body:
                out[key] = body[key]
        return out


class _BedrockCohereAdapter:
    name = "aws.bedrock.cohere"

    def adapt(self, body: dict[str, Any], url: str) -> dict[str, Any]:
        body = _safe_dict(body)
        out: dict[str, Any] = {"model": _model_id_from_bedrock_url(url) or "unknown"}
        # Cohere uses a single-string prompt; synthesize a one-message array.
        if "prompt" in body and isinstance(body["prompt"], str):
            out["messages"] = [{"role": "user", "content": body["prompt"]}]
        if "max_tokens" in body:
            out["max_tokens"] = body["max_tokens"]
        if "temperature" in body:
            out["temperature"] = body["temperature"]
        # Cohere uses `p` for top_p and `k` for top_k.
        if "p" in body:
            out["top_p"] = body["p"]
        if "k" in body:
            out["top_k"] = body["k"]
        if "stop_sequences" in body:
            out["stop_sequences"] = body["stop_sequences"]
        return out


class _BedrockTitanAdapter:
    name = "aws.bedrock.titan"

    def adapt(self, body: dict[str, Any], url: str) -> dict[str, Any]:
        body = _safe_dict(body)
        out: dict[str, Any] = {"model": _model_id_from_bedrock_url(url) or "unknown"}
        if "inputText" in body and isinstance(body["inputText"], str):
            out["messages"] = [{"role": "user", "content": body["inputText"]}]
        # Titan nests sampling params under textGenerationConfig.
        cfg = body.get("textGenerationConfig")
        if isinstance(cfg, dict):
            if "maxTokenCount" in cfg:
                out["max_tokens"] = cfg["maxTokenCount"]
            if "temperature" in cfg:
                out["temperature"] = cfg["temperature"]
            if "topP" in cfg:
                out["top_p"] = cfg["topP"]
            if "stopSequences" in cfg:
                out["stop_sequences"] = cfg["stopSequences"]
        return out


class _BedrockLlamaAdapter:
    name = "aws.bedrock.llama"

    def adapt(self, body: dict[str, Any], url: str) -> dict[str, Any]:
        body = _safe_dict(body)
        out: dict[str, Any] = {"model": _model_id_from_bedrock_url(url) or "unknown"}
        if "prompt" in body and isinstance(body["prompt"], str):
            out["messages"] = [{"role": "user", "content": body["prompt"]}]
        # Llama uses max_gen_len for max_tokens.
        if "max_gen_len" in body:
            out["max_tokens"] = body["max_gen_len"]
        if "temperature" in body:
            out["temperature"] = body["temperature"]
        if "top_p" in body:
            out["top_p"] = body["top_p"]
        return out


# ── Registry ─────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, AdapterProtocol] = {
    a.name: a for a in (
        _BedrockAnthropicAdapter(),
        _BedrockCohereAdapter(),
        _BedrockTitanAdapter(),
        _BedrockLlamaAdapter(),
    )
}


def known_adapter_names() -> list[str]:
    return sorted(_REGISTRY.keys())


def get_adapter(name: str) -> AdapterProtocol:
    """Return the adapter registered under ``name``.

    Raises :class:`UnknownAdapterError` if ``name`` is unknown.
    """
    a = _REGISTRY.get(name)
    if a is None:
        raise UnknownAdapterError(
            f"unknown body adapter: {name!r}. "
            f"Available: {', '.join(known_adapter_names())}"
        )
    return a


def register_adapter(adapter: AdapterProtocol) -> None:
    """Register a runtime adapter. Plugins discovered via the
    ``novafabric.body_adapters`` entry-point group call this.

    Raises :class:`ValueError` if an adapter with the same name is
    already registered (use :func:`unregister_adapter` first to swap)."""
    if adapter.name in _REGISTRY:
        raise ValueError(f"adapter {adapter.name!r} is already registered")
    _REGISTRY[adapter.name] = adapter


def unregister_adapter(name: str) -> None:
    """Remove a runtime-registered adapter. No-op if not present."""
    _REGISTRY.pop(name, None)


# ── Dispatcher: the wire-level hooks call this ───────────────────────────────


def _looks_already_openai_shaped(body: dict[str, Any]) -> bool:
    """Idempotency detector: a body that already has both ``model`` AND
    ``messages`` is presumed already-normalized (or natively OpenAI-shaped)
    — the adapter would be a no-op or destructive."""
    return "model" in body and "messages" in body


def adapt_body(body: Any, *, url: str = "") -> dict[str, Any]:
    """Normalize ``body`` for ``url`` if a registered adapter applies.

    Idempotent: if ``body`` already has both ``model`` and ``messages``
    (the OpenAI shape the adapter would produce), it's returned
    unchanged — calling this twice is safe.

    If no adapter is registered for the URL, the body is returned
    unchanged (passthrough — the generic OpenAI-shape extractor handles
    OpenAI/Anthropic/etc. natively).

    Never raises. On non-dict input, returns ``{}`` so the downstream
    extractor sees a safe value.
    """
    safe: dict[str, Any] = body if isinstance(body, dict) else {}
    if _looks_already_openai_shaped(safe):
        return dict(safe)
    adapter_name = _resolve_adapter_name_for_url(url)
    if adapter_name is None:
        return safe
    try:
        adapter = get_adapter(adapter_name)
    except UnknownAdapterError:
        return safe
    try:
        return adapter.adapt(safe, url)
    except Exception:
        # Adapter must not break capture. Fall back to the raw body.
        return safe
