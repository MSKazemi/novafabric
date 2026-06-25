from __future__ import annotations

import functools
import hashlib
import json
import types
from typing import Any


def _arg_hash(arguments: Any) -> str:
    if not isinstance(arguments, dict):
        arguments = {}
    return hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()[:16]


def _mock_openai_response(stored: dict[str, Any]) -> Any:
    choices = []
    for c in stored.get("gen_ai.response.choices", []):
        msg = types.SimpleNamespace(
            role=c.get("message", {}).get("role", "assistant"),
            content=c.get("message", {}).get("content", ""),
        )
        choices.append(types.SimpleNamespace(
            index=c.get("index", 0),
            message=msg,
            finish_reason=c.get("finish_reason", "stop"),
        ))
    usage = types.SimpleNamespace(
        prompt_tokens=stored.get("gen_ai.usage.input_tokens", 0),
        completion_tokens=stored.get("gen_ai.usage.output_tokens", 0),
        total_tokens=(
            stored.get("gen_ai.usage.input_tokens", 0)
            + stored.get("gen_ai.usage.output_tokens", 0)
        ),
    )
    return types.SimpleNamespace(
        id="replay-mocked",
        model=stored.get("gen_ai.response.model", ""),
        choices=choices,
        usage=usage,
    )


def _mock_anthropic_response(stored: dict[str, Any]) -> Any:
    choices = stored.get("gen_ai.response.choices", [])
    content_text = choices[0].get("message", {}).get("content", "") if choices else ""
    finish_reason = choices[0].get("finish_reason", "end_turn") if choices else "end_turn"
    content_block = types.SimpleNamespace(type="text", text=content_text)
    usage = types.SimpleNamespace(
        input_tokens=stored.get("gen_ai.usage.input_tokens", 0),
        output_tokens=stored.get("gen_ai.usage.output_tokens", 0),
    )
    return types.SimpleNamespace(
        id="replay-mocked",
        model=stored.get("gen_ai.response.model", ""),
        content=[content_block],
        stop_reason=finish_reason,
        usage=usage,
        type="message",
        role="assistant",
    )


class MockModelDispatcher:
    """Intercept OpenAI/Anthropic create() calls and return stored responses in order."""

    def __init__(self, model_calls: list[dict[str, Any]]) -> None:
        self._openai_queue: list[dict[str, Any]] = [
            r for r in model_calls if r.get("gen_ai.system") == "openai"
        ]
        self._anthropic_queue: list[dict[str, Any]] = [
            r for r in model_calls if r.get("gen_ai.system") == "anthropic"
        ]
        self._openai_original: Any = None
        self._anthropic_original: Any = None
        self._openai_index = 0
        self._anthropic_index = 0

    def install(self) -> None:
        self._install_openai()
        self._install_anthropic()

    def uninstall(self) -> None:
        self._uninstall_openai()
        self._uninstall_anthropic()

    def _install_openai(self) -> None:
        try:
            import openai.resources.chat.completions as _mod
            self._openai_original = _mod.Completions.create
            dispatcher = self

            @functools.wraps(self._openai_original)
            def mock_create(inner_self: Any, *args: Any, **kwargs: Any) -> Any:
                idx = dispatcher._openai_index
                dispatcher._openai_index += 1
                if idx < len(dispatcher._openai_queue):
                    return _mock_openai_response(dispatcher._openai_queue[idx])
                return _mock_openai_response({})

            _mod.Completions.create = mock_create  # type: ignore[method-assign, assignment]
        except (ImportError, AttributeError):
            pass

    def _uninstall_openai(self) -> None:
        if self._openai_original is None:
            return
        try:
            import openai.resources.chat.completions as _mod
            _mod.Completions.create = self._openai_original  # type: ignore[method-assign]
        except (ImportError, AttributeError):
            pass
        finally:
            self._openai_original = None

    def _install_anthropic(self) -> None:
        try:
            import anthropic.resources.messages as _mod  # type: ignore[import-not-found]
            self._anthropic_original = _mod.Messages.create
            dispatcher = self

            @functools.wraps(self._anthropic_original)
            def mock_create(inner_self: Any, *args: Any, **kwargs: Any) -> Any:
                idx = dispatcher._anthropic_index
                dispatcher._anthropic_index += 1
                if idx < len(dispatcher._anthropic_queue):
                    return _mock_anthropic_response(dispatcher._anthropic_queue[idx])
                return _mock_anthropic_response({})

            _mod.Messages.create = mock_create
        except (ImportError, AttributeError):
            pass

    def _uninstall_anthropic(self) -> None:
        if self._anthropic_original is None:
            return
        try:
            import anthropic.resources.messages as _mod
            _mod.Messages.create = self._anthropic_original
        except (ImportError, AttributeError):
            pass
        finally:
            self._anthropic_original = None


class MockToolDispatcher:
    """Return stored tool results; match by tool_call_id first, then (tool_name, arg_hash)."""

    def __init__(self, tool_calls: list[dict[str, Any]]) -> None:
        self._by_id: dict[str, dict[str, Any]] = {
            r["tool_call_id"]: r for r in tool_calls if "tool_call_id" in r
        }
        self._by_sig: dict[tuple[str, str], dict[str, Any]] = {
            (r["tool_name"], _arg_hash(r.get("arguments", {}))): r
            for r in tool_calls if "tool_name" in r
        }

    def lookup(
        self, tool_call_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        return self._by_id.get(tool_call_id) or self._by_sig.get(
            (tool_name, _arg_hash(arguments))
        )
