from __future__ import annotations

import functools
import hashlib
import json
import sys
import types
from typing import Any


def _arg_hash(arguments: Any) -> str:
    if not isinstance(arguments, dict):
        arguments = {}
    return hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()[:16]


def _arguments_json(arguments: Any) -> str:
    # Inverse of capture's parsing: an argument string the model emitted as
    # invalid JSON was kept under "_unparsed" and is served back verbatim.
    if isinstance(arguments, dict) and set(arguments) == {"_unparsed"}:
        return str(arguments["_unparsed"])
    return json.dumps(arguments if isinstance(arguments, dict) else {})


def _openai_tool_calls(message: dict[str, Any]) -> list[Any] | None:
    refs = message.get("tool_calls")
    if not isinstance(refs, list) or not refs:
        return None
    return [
        types.SimpleNamespace(
            id=ref.get("id", ""),
            type="function",
            function=types.SimpleNamespace(
                name=ref.get("name", ""),
                arguments=_arguments_json(ref.get("arguments")),
            ),
        )
        for ref in refs
        if isinstance(ref, dict)
    ]


def _mock_openai_response(stored: dict[str, Any]) -> Any:
    choices = []
    for c in stored.get("gen_ai.response.choices", []):
        message = c.get("message", {})
        msg = types.SimpleNamespace(
            role=message.get("role", "assistant"),
            content=message.get("content", ""),
            tool_calls=_openai_tool_calls(message),
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


# A record may carry the OTel finish-reason enum (model-call.schema.json) rather
# than Anthropic's own stop_reason vocabulary; an Anthropic client expects the latter.
_ANTHROPIC_STOP_REASON = {
    "tool_calls": "tool_use",
    "stop": "end_turn",
    "length": "max_tokens",
}


def _mock_anthropic_response(stored: dict[str, Any]) -> Any:
    choices = stored.get("gen_ai.response.choices", [])
    message = choices[0].get("message", {}) if choices else {}
    content_text = message.get("content", "") or ""
    finish_reason = choices[0].get("finish_reason", "end_turn") if choices else "end_turn"
    blocks: list[Any] = []
    if content_text:
        blocks.append(types.SimpleNamespace(type="text", text=content_text))
    for ref in message.get("tool_calls") or []:
        if isinstance(ref, dict):
            arguments = ref.get("arguments")
            blocks.append(types.SimpleNamespace(
                type="tool_use",
                id=ref.get("id", ""),
                name=ref.get("name", ""),
                input=arguments if isinstance(arguments, dict) else {},
            ))
    if not blocks:
        blocks.append(types.SimpleNamespace(type="text", text=""))
    usage = types.SimpleNamespace(
        input_tokens=stored.get("gen_ai.usage.input_tokens", 0),
        output_tokens=stored.get("gen_ai.usage.output_tokens", 0),
    )
    return types.SimpleNamespace(
        id="replay-mocked",
        model=stored.get("gen_ai.response.model", ""),
        content=blocks,
        stop_reason=_ANTHROPIC_STOP_REASON.get(finish_reason, finish_reason),
        usage=usage,
        type="message",
        role="assistant",
    )


def _warn_exhausted(provider: str, index: int, recorded: int) -> None:
    # Serving a blank reply keeps the replay running, but it must not be silent:
    # the replayed agent made more calls than the capsule recorded.
    print(
        f"[novafabric] mocked replay: no recorded response left for {provider} call "
        f"#{index + 1} (capsule recorded {recorded}); serving an empty response",
        file=sys.stderr,
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
                _warn_exhausted("openai", idx, len(dispatcher._openai_queue))
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
                _warn_exhausted("anthropic", idx, len(dispatcher._anthropic_queue))
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
