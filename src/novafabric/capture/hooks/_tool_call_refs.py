"""Assistant tool-call requests as ``Message.tool_calls`` (model-call.schema.json).

``ToolCallRef`` is ``{id, name, arguments: object}`` for both providers. OpenAI sends
arguments as a JSON string: it is parsed, and a string the model emitted as invalid
JSON is kept verbatim under ``_unparsed`` so replay can serve it back unchanged.
Only real lists / real ``"tool_use"`` blocks are read, never duck-typed guesses.

This is the one implementation of that shape: the direct SDK hooks and the API
proxy (streaming and Anthropic-synthesized responses) all call it, so every capture
path emits the same canonical form.

Missing-name policy (issue #12): a tool-call entry whose ``name`` is absent or not a
non-empty string cannot be served back by replay (the schema requires ``name``), so
it is **dropped from** ``tool_calls`` -- but never silently. The number dropped is
recorded on the model-call record under
``extensions["io.novafabric.tool_calls_dropped"]`` and a warning is logged.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Reverse-DNS extension key: tool-call entries dropped because they had no name.
TOOL_CALLS_DROPPED_EXT = "io.novafabric.tool_calls_dropped"


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def parse_tool_arguments(raw: Any) -> dict[str, Any]:
    """Parse a provider's tool arguments into the canonical object form."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return {"_unparsed": raw}
        return parsed if isinstance(parsed, dict) else {"_unparsed": raw}
    return {}


def _named(name: Any) -> bool:
    return isinstance(name, str) and bool(name)


def openai_tool_call_refs_with_dropped(message: Any) -> tuple[list[dict[str, Any]], int]:
    """Canonical refs from an OpenAI message, plus how many nameless entries were dropped."""
    calls = _get(message, "tool_calls")
    if not isinstance(calls, (list, tuple)):
        return [], 0
    refs: list[dict[str, Any]] = []
    dropped = 0
    for call in calls:
        function = _get(call, "function")
        name = _get(function, "name")
        if not _named(name):
            dropped += 1
            continue
        refs.append({
            "id": str(_get(call, "id") or ""),
            "name": name,
            "arguments": parse_tool_arguments(_get(function, "arguments")),
        })
    return refs, dropped


def anthropic_tool_call_refs_with_dropped(content: Any) -> tuple[list[dict[str, Any]], int]:
    """Canonical refs from Anthropic content blocks, plus nameless entries dropped."""
    if not isinstance(content, (list, tuple)):
        return [], 0
    refs: list[dict[str, Any]] = []
    dropped = 0
    for block in content:
        if _get(block, "type") != "tool_use":
            continue
        name = _get(block, "name")
        if not _named(name):
            dropped += 1
            continue
        refs.append({
            "id": str(_get(block, "id") or ""),
            "name": name,
            "arguments": parse_tool_arguments(_get(block, "input")),
        })
    return refs, dropped


def openai_tool_call_refs(message: Any) -> list[dict[str, Any]]:
    return openai_tool_call_refs_with_dropped(message)[0]


def anthropic_tool_call_refs(content: Any) -> list[dict[str, Any]]:
    return anthropic_tool_call_refs_with_dropped(content)[0]


def note_dropped_tool_calls(record: dict[str, Any], dropped: int) -> None:
    """Record (and log) nameless tool-call entries a capture path had to drop."""
    if dropped <= 0:
        return
    ext = record.setdefault("extensions", {})
    ext[TOOL_CALLS_DROPPED_EXT] = int(ext.get(TOOL_CALLS_DROPPED_EXT, 0)) + dropped
    logger.warning(
        "novafabric capture: dropped %d tool-call entr%s without a name from a "
        "%s response; counted under extensions[%r]",
        dropped,
        "y" if dropped == 1 else "ies",
        record.get("gen_ai.system", "model"),
        TOOL_CALLS_DROPPED_EXT,
    )
