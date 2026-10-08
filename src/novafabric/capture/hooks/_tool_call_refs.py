"""Assistant tool-call requests as ``Message.tool_calls`` (model-call.schema.json).

``ToolCallRef`` is ``{id, name, arguments: object}`` for both providers. OpenAI sends
arguments as a JSON string: it is parsed, and a string the model emitted as invalid
JSON is kept verbatim under ``_unparsed`` so replay can serve it back unchanged.
Only real lists / real ``"tool_use"`` blocks are read, never duck-typed guesses.
"""

from __future__ import annotations

import json
from typing import Any


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return {"_unparsed": raw}
        return parsed if isinstance(parsed, dict) else {"_unparsed": raw}
    return {}


def openai_tool_call_refs(message: Any) -> list[dict[str, Any]]:
    calls = _get(message, "tool_calls")
    if not isinstance(calls, (list, tuple)):
        return []
    refs: list[dict[str, Any]] = []
    for call in calls:
        function = _get(call, "function")
        name = _get(function, "name")
        if not isinstance(name, str):
            continue
        refs.append({
            "id": str(_get(call, "id") or ""),
            "name": name,
            "arguments": _parse_arguments(_get(function, "arguments")),
        })
    return refs


def anthropic_tool_call_refs(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, (list, tuple)):
        return []
    refs: list[dict[str, Any]] = []
    for block in content:
        if _get(block, "type") != "tool_use":
            continue
        name = _get(block, "name")
        if not isinstance(name, str):
            continue
        refs.append({
            "id": str(_get(block, "id") or ""),
            "name": name,
            "arguments": _parse_arguments(_get(block, "input")),
        })
    return refs
