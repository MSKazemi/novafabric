"""Provider finish/stop reasons mapped onto the model-call schema enum.

``Choice.finish_reason`` and ``gen_ai.response.finish_reasons`` in
model-call.schema.json accept only ``stop | length | content_filter | tool_calls |
error``. Anthropic reports its own ``stop_reason`` vocabulary (``end_turn``,
``tool_use``, ...), which the Anthropic hook and the API proxy used to store
verbatim -- outside the enum. Capture now stores the canonical value and keeps
the provider's own value, additively, under
``extensions["io.novafabric.provider_finish_reasons"]`` so mocked replay can
serve back exactly what the provider said.
"""

from __future__ import annotations

from typing import Any

#: Reverse-DNS extension key carrying the provider's raw finish reasons, one per
#: choice, when they differ from the canonical enum values.
PROVIDER_FINISH_REASONS_EXT = "io.novafabric.provider_finish_reasons"

#: Anthropic ``stop_reason`` -> schema enum. ``pause_turn`` and ``stop_sequence``
#: are normal ends of a turn, so they map to ``stop``; the raw value survives in
#: the extension. An unrecognised value also maps to ``stop`` (never dropped:
#: the raw value is kept beside it).
_ANTHROPIC_TO_CANONICAL = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "pause_turn": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}

CANONICAL_FINISH_REASONS = frozenset(
    {"stop", "length", "content_filter", "tool_calls", "error"}
)


def canonical_finish_reason(provider: str, raw: Any) -> str:
    """Return the schema-enum finish reason for a provider's raw value."""
    value = str(raw) if raw else "stop"
    if provider == "anthropic":
        if value in CANONICAL_FINISH_REASONS:
            return value
        return _ANTHROPIC_TO_CANONICAL.get(value, "stop")
    # OpenAI's vocabulary is where the enum comes from; the legacy
    # ``function_call`` reason is the pre-tools spelling of ``tool_calls``.
    if value == "function_call":
        return "tool_calls"
    return value


def attach_provider_finish_reasons(
    record: dict[str, Any], raw: list[str], canonical: list[str]
) -> None:
    """Keep the provider's own values when they differ from the canonical ones."""
    if raw and raw != canonical:
        record.setdefault("extensions", {})[PROVIDER_FINISH_REASONS_EXT] = list(raw)
