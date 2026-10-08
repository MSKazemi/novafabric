"""In-process dispatchers for mocked replay (ADR-0300).

Installed into the replayed Python process by the ``sitecustomize.py`` the replay
engine writes (see :func:`install_from_env`):

* :class:`MockModelDispatcher` serves recorded model responses on the supported
  surfaces (``MODEL_SURFACES``) and guards the unsupported ones
  (``UNSUPPORTED_MODEL_SURFACES``) so they cannot silently go live;
* :class:`MockToolDispatcher` serves recorded MCP ``tools/call`` results through
  ``mcp.ClientSession.call_tool`` -- the one tool surface NovaFabric intercepts.

Every action is appended to an event log the engine reads afterwards. Under the
default ``fail`` divergence policy a call with no recorded answer raises a
:class:`~novafabric.replay._errors.ReplayDivergenceError` subclass; under the
opt-in ``warn`` policy (``nova replay --permissive``) the pre-ADR-0300 behaviour
is kept -- an empty model response, live tools -- and the divergence is still
recorded.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import json
import os
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

from novafabric.replay._contract import (
    MODEL_SURFACES,
    TOOL_SURFACE_MCP,
    ReplayEventLog,
    ToolCallMatcher,
    interceptable_tool_calls,
    model_queues,
    normalized_arg_hash,
    recorded_provider_order,
)
from novafabric.replay._errors import (
    ReplayDivergenceError,
    ReplayOrderMismatchError,
    ReplayProviderMismatchError,
    ReplayQueueExhaustedError,
    ReplayRecordedToolError,
    ReplayRecordMalformedError,
    ReplayToolUnmatchedError,
    ReplayUnsupportedSurfaceError,
)

#: Exit status of a strict replay whose dispatcher could not be installed: the
#: process is stopped before the workload runs, because running it would send
#: every model call to the network under a "mocked" label.
REPLAY_DISPATCHER_UNAVAILABLE_EXIT = 86

#: Provider-finish-reason extension written at capture (issue #12).
_PROVIDER_FINISH_REASONS_EXT = "io.novafabric.provider_finish_reasons"

#: Model API surfaces mocked replay does NOT serve. Strict replay refuses them;
#: permissive replay lets them run live and counts them. Each entry is
#: (provider, module, class, attribute, human-readable surface).
UNSUPPORTED_MODEL_SURFACES: tuple[tuple[str, str, str, str, str], ...] = (
    ("openai", "openai.resources.chat.completions", "Completions", "parse",
     "openai.chat.completions.parse (structured outputs)"),
    ("openai", "openai.resources.chat.completions", "AsyncCompletions", "create",
     "openai.chat.completions.create (async)"),
    ("openai", "openai.resources.chat.completions", "AsyncCompletions", "parse",
     "openai.chat.completions.parse (async)"),
    ("openai", "openai.resources.responses", "Responses", "create",
     "openai.responses.create (Responses API)"),
    ("openai", "openai.resources.responses", "Responses", "parse",
     "openai.responses.parse (Responses API)"),
    ("openai", "openai.resources.responses", "AsyncResponses", "create",
     "openai.responses.create (Responses API, async)"),
    ("openai", "openai.resources.responses", "AsyncResponses", "parse",
     "openai.responses.parse (Responses API, async)"),
    ("openai", "openai.resources.completions", "Completions", "create",
     "openai.completions.create (legacy text completions)"),
    ("openai", "openai.resources.completions", "AsyncCompletions", "create",
     "openai.completions.create (legacy text completions, async)"),
    ("anthropic", "anthropic.resources.messages", "Messages", "stream",
     "anthropic.messages.stream"),
    ("anthropic", "anthropic.resources.messages", "AsyncMessages", "create",
     "anthropic.messages.create (async)"),
    ("anthropic", "anthropic.resources.messages", "AsyncMessages", "stream",
     "anthropic.messages.stream (async)"),
)

_SUPPORTED_CREATE: dict[str, tuple[str, str]] = {
    "openai": ("openai.resources.chat.completions", "Completions"),
    "anthropic": ("anthropic.resources.messages", "Messages"),
}


# ── response reconstruction ──────────────────────────────────────────────────


def _arg_hash(arguments: Any) -> str:
    """Kept for callers of the pre-ADR-0300 name; see ``normalized_arg_hash``."""
    return normalized_arg_hash(arguments)


def _arguments_json(arguments: Any) -> str:
    # Inverse of capture's parsing: an argument string the model emitted as
    # invalid JSON was kept under "_unparsed" and is served back verbatim.
    if isinstance(arguments, dict) and set(arguments) == {"_unparsed"}:
        return str(arguments["_unparsed"])
    return json.dumps(arguments if isinstance(arguments, dict) else {})


def _well_formed_ref(ref: Any) -> bool:
    return isinstance(ref, dict) and isinstance(ref.get("name"), str) and bool(ref["name"])


def _malformed_tool_call_refs(stored: dict[str, Any]) -> int:
    """Stored tool-call entries replay cannot rebuild (no ``name``)."""
    count = 0
    for choice in stored.get("gen_ai.response.choices") or []:
        message = choice.get("message", {}) if isinstance(choice, dict) else {}
        refs = message.get("tool_calls") if isinstance(message, dict) else None
        if isinstance(refs, list):
            count += sum(1 for ref in refs if not _well_formed_ref(ref))
    return count


def _openai_tool_calls(message: dict[str, Any]) -> list[Any] | None:
    refs = message.get("tool_calls")
    if not isinstance(refs, list) or not refs:
        return None
    calls = [
        types.SimpleNamespace(
            id=ref.get("id", ""),
            type="function",
            function=types.SimpleNamespace(
                name=ref["name"],
                arguments=_arguments_json(ref.get("arguments")),
            ),
        )
        for ref in refs
        if _well_formed_ref(ref)
    ]
    return calls or None


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
        id=stored.get("gen_ai.response.id", "replay-mocked"),
        model=stored.get("gen_ai.response.model", ""),
        choices=choices,
        usage=usage,
    )


# A record carries the schema's finish-reason enum (model-call.schema.json); an
# Anthropic client expects Anthropic's own stop_reason vocabulary.
_ANTHROPIC_STOP_REASON = {
    "tool_calls": "tool_use",
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "refusal",
}


def _anthropic_stop_reason(stored: dict[str, Any], finish_reason: str) -> str:
    # Capture keeps Anthropic's raw value additively (issue #12); serve exactly
    # that when present, so e.g. stop_sequence is not flattened to end_turn.
    ext = stored.get("extensions")
    if isinstance(ext, dict):
        raw = ext.get(_PROVIDER_FINISH_REASONS_EXT)
        if isinstance(raw, list) and raw and isinstance(raw[0], str):
            return raw[0]
    return _ANTHROPIC_STOP_REASON.get(finish_reason, finish_reason)


def _mock_anthropic_response(stored: dict[str, Any]) -> Any:
    choices = stored.get("gen_ai.response.choices", [])
    message = choices[0].get("message", {}) if choices else {}
    content_text = message.get("content", "") or ""
    finish_reason = choices[0].get("finish_reason", "end_turn") if choices else "end_turn"
    blocks: list[Any] = []
    if content_text:
        blocks.append(types.SimpleNamespace(type="text", text=content_text))
    for ref in message.get("tool_calls") or []:
        if _well_formed_ref(ref):
            arguments = ref.get("arguments")
            blocks.append(types.SimpleNamespace(
                type="tool_use",
                id=ref.get("id", ""),
                name=ref["name"],
                input=arguments if isinstance(arguments, dict) else {},
            ))
    if not blocks:
        blocks.append(types.SimpleNamespace(type="text", text=""))
    usage = types.SimpleNamespace(
        input_tokens=stored.get("gen_ai.usage.input_tokens", 0),
        output_tokens=stored.get("gen_ai.usage.output_tokens", 0),
    )
    return types.SimpleNamespace(
        id=stored.get("gen_ai.response.id", "replay-mocked"),
        model=stored.get("gen_ai.response.model", ""),
        content=blocks,
        stop_reason=_anthropic_stop_reason(stored, finish_reason),
        usage=usage,
        type="message",
        role="assistant",
    )


_BUILDERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "openai": _mock_openai_response,
    "anthropic": _mock_anthropic_response,
}


# ── shared plumbing ──────────────────────────────────────────────────────────


def _report_divergence(
    events: ReplayEventLog, policy: str, error: ReplayDivergenceError
) -> None:
    """Record a divergence; raise it under ``fail``, warn on stderr under ``warn``."""
    events.emit("divergence", policy=policy, **error.as_dict())
    if policy != "warn":
        raise error
    print(f"[novafabric] mocked replay (permissive): {error}", file=sys.stderr)


class _Patcher:
    """Patch class attributes and restore them exactly (own vs inherited)."""

    def __init__(self) -> None:
        self._saved: list[tuple[type, str, Any, bool]] = []

    def patch(
        self, module_name: str, class_name: str, attr: str,
        factory: Callable[[Any], Any],
    ) -> bool:
        try:
            module = importlib.import_module(module_name)
            owner = getattr(module, class_name)
            original = getattr(owner, attr)
        except (ImportError, AttributeError):
            return False
        had_own = attr in vars(owner)
        setattr(owner, attr, factory(original))
        self._saved.append((owner, attr, original, had_own))
        return True

    def restore(self) -> None:
        while self._saved:
            owner, attr, original, had_own = self._saved.pop()
            if had_own:
                setattr(owner, attr, original)
            else:
                try:
                    delattr(owner, attr)
                except AttributeError:
                    pass


# ── model calls ──────────────────────────────────────────────────────────────


class MockModelDispatcher:
    """Serve recorded OpenAI/Anthropic responses in recorded order.

    One queue per provider, built from the servable records only
    (``_contract.model_queues``). ``divergence_policy="fail"`` (the default)
    raises on a call with no recorded answer; ``"warn"`` serves an empty
    response and warns, as replay did before ADR-0300.
    """

    def __init__(
        self,
        model_calls: list[dict[str, Any]],
        *,
        divergence_policy: str = "fail",
        events: ReplayEventLog | None = None,
    ) -> None:
        self._queues = model_queues(model_calls)
        self._order = recorded_provider_order(model_calls)
        self._index: dict[str, int] = dict.fromkeys(self._queues, 0)
        self._global_index = 0
        self._policy = divergence_policy
        self._events = events or ReplayEventLog(None)
        self._patcher = _Patcher()
        self.installed_surfaces: list[str] = []

    # Kept for introspection by existing callers/tests.
    @property
    def _openai_queue(self) -> list[dict[str, Any]]:
        return self._queues["openai"]

    @property
    def _anthropic_queue(self) -> list[dict[str, Any]]:
        return self._queues["anthropic"]

    @property
    def _openai_index(self) -> int:
        return self._index["openai"]

    @property
    def _anthropic_index(self) -> int:
        return self._index["anthropic"]

    def install(self) -> None:
        for provider, (module_name, class_name) in _SUPPORTED_CREATE.items():
            if self._patcher.patch(
                module_name, class_name, "create",
                functools.partial(self._make_create, provider),
            ):
                self.installed_surfaces.append(MODEL_SURFACES[provider])
        for provider, module_name, class_name, attr, surface in UNSUPPORTED_MODEL_SURFACES:
            self._patcher.patch(
                module_name, class_name, attr,
                functools.partial(self._make_guard, provider, surface),
            )

    def uninstall(self) -> None:
        self._patcher.restore()
        self.installed_surfaces = []

    def _make_create(self, provider: str, original: Any) -> Any:
        dispatcher = self

        @functools.wraps(original)
        def mock_create(inner_self: Any, *args: Any, **kwargs: Any) -> Any:
            if kwargs.get("stream"):
                dispatcher._unsupported(provider, f"{MODEL_SURFACES[provider]} with stream=True")
                return original(inner_self, *args, **kwargs)
            return dispatcher._next_response(provider)

        return mock_create

    def _make_guard(self, provider: str, surface: str, original: Any) -> Any:
        dispatcher = self
        if inspect.iscoroutinefunction(original):

            @functools.wraps(original)
            async def async_guard(inner_self: Any, *args: Any, **kwargs: Any) -> Any:
                dispatcher._unsupported(provider, surface)
                return await original(inner_self, *args, **kwargs)

            return async_guard

        @functools.wraps(original)
        def guard(inner_self: Any, *args: Any, **kwargs: Any) -> Any:
            dispatcher._unsupported(provider, surface)
            return original(inner_self, *args, **kwargs)

        return guard

    def _unsupported(self, provider: str, surface: str) -> None:
        """Refuse (fail) or let through live and count it (warn)."""
        _report_divergence(self._events, self._policy, ReplayUnsupportedSurfaceError(
            f"{surface} is not served by mocked replay; "
            + ("refused" if self._policy != "warn" else "running it live"),
            provider=provider,
            surface=surface,
        ))
        self._events.emit("model_live", provider=provider, surface=surface)

    def _next_response(self, provider: str) -> Any:
        queue = self._queues[provider]
        idx = self._index[provider]
        build = _BUILDERS[provider]
        if idx >= len(queue):
            self._index[provider] += 1
            others = {
                p: len(q) - self._index[p]
                for p, q in self._queues.items()
                if p != provider and self._index[p] < len(q)
            }
            cls = ReplayProviderMismatchError if others else ReplayQueueExhaustedError
            detail = (
                f"; recorded responses remain unconsumed for {sorted(others)}"
                if others else ""
            )
            _report_divergence(self._events, self._policy, cls(
                f"no recorded {provider} response left for call #{idx + 1} "
                f"(capsule recorded {len(queue)}){detail}"
                + ("; serving an empty response" if self._policy == "warn" else ""),
                provider=provider,
                call_index=idx,
                recorded_queue_length=len(queue),
                surface=MODEL_SURFACES[provider],
            ))
            return build({})
        position = self._global_index
        if position < len(self._order) and self._order[position] != provider:
            _report_divergence(self._events, self._policy, ReplayOrderMismatchError(
                f"call #{position + 1} went to {provider}; the capsule recorded "
                f"{self._order[position]} at that position",
                provider=provider,
                expected_provider=self._order[position],
                global_call_index=position,
            ))
        record = queue[idx]
        malformed = _malformed_tool_call_refs(record)
        if malformed:
            _report_divergence(self._events, self._policy, ReplayRecordMalformedError(
                f"recorded {provider} response #{idx + 1} has {malformed} tool-call "
                "entr" + ("y" if malformed == 1 else "ies") + " without a name; "
                + ("served without them" if self._policy == "warn" else "refusing to serve it"),
                provider=provider,
                call_index=idx,
                model_call_id=record.get("model_call_id"),
            ))
        self._index[provider] += 1
        self._global_index += 1
        self._events.emit(
            "model_served",
            provider=provider,
            call_index=idx,
            model_call_id=record.get("model_call_id"),
        )
        return build(record)


# ── tool calls ───────────────────────────────────────────────────────────────


def _raise_recorded_tool_error(record: dict[str, Any], envelope: Any) -> None:
    rpc_error = envelope.get("error") if isinstance(envelope, dict) else None
    if isinstance(rpc_error, dict):
        try:
            from mcp.shared.exceptions import McpError
            from mcp.types import ErrorData

            raise McpError(ErrorData(
                code=int(rpc_error.get("code", -32603)),
                message=str(rpc_error.get("message", "")),
                data=rpc_error.get("data"),
            ))
        except ImportError:
            pass
        raise ReplayRecordedToolError("McpError", str(rpc_error.get("message", "")))
    raw_error = record.get("error")
    error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
    raise ReplayRecordedToolError(
        str(error.get("type", "")), str(error.get("message", "recorded tool call failed"))
    )


def _mcp_result_from_record(record: dict[str, Any]) -> Any:
    """Rebuild the ``CallToolResult`` a recorded MCP ``tools/call`` returned.

    The JSON-RPC response envelope (verbatim under ``nova mcp-proxy``) is
    preferred over the hook's serialized ``result``.
    """
    raw_mcp = record.get("mcp")
    mcp: dict[str, Any] = raw_mcp if isinstance(raw_mcp, dict) else {}
    envelope = mcp.get("response_envelope")
    if record.get("status") not in (None, "success") or (
        isinstance(envelope, dict) and "error" in envelope
    ):
        _raise_recorded_tool_error(record, envelope)
    payload: Any = None
    if isinstance(envelope, dict) and isinstance(envelope.get("result"), dict):
        payload = envelope["result"]
    elif isinstance(record.get("result"), dict):
        payload = record["result"]
    payload = payload or {"content": []}
    try:
        from mcp.types import CallToolResult

        return CallToolResult.model_validate(payload)
    except Exception:  # noqa: BLE001 -- lossy record or no mcp SDK: rebuild by attribute
        content = [
            types.SimpleNamespace(**item) if isinstance(item, dict) else item
            for item in payload.get("content") or []
        ]
        return types.SimpleNamespace(
            content=content,
            isError=bool(payload.get("isError", False)),
            structuredContent=payload.get("structuredContent"),
            meta=None,
        )


class MockToolDispatcher:
    """Serve recorded MCP ``tools/call`` results, one-to-one (ADR-0300).

    Only records on the intercepted surface (``_contract.interceptable_tool_calls``)
    are matchable. A call with no unconsumed record is refused under ``fail``
    -- the live tool is never executed -- and runs live under ``warn``.
    """

    def __init__(
        self,
        tool_calls: list[dict[str, Any]],
        *,
        divergence_policy: str = "fail",
        events: ReplayEventLog | None = None,
    ) -> None:
        self._matcher = ToolCallMatcher(interceptable_tool_calls(tool_calls))
        self._policy = divergence_policy
        self._events = events or ReplayEventLog(None)
        self._patcher = _Patcher()
        self.installed_surfaces: list[str] = []

    def lookup(
        self, tool_call_id: str | None, tool_name: str, arguments: Any
    ) -> dict[str, Any] | None:
        """Match and CONSUME one recorded call; ``None`` when unmatched."""
        return self._matcher.match(tool_call_id, tool_name, arguments).record

    def install(self) -> None:
        if self._patcher.patch(
            "mcp.client.session", "ClientSession", "call_tool", self._make_call_tool
        ):
            self.installed_surfaces.append(TOOL_SURFACE_MCP)

    def uninstall(self) -> None:
        self._patcher.restore()
        self.installed_surfaces = []

    def _make_call_tool(self, original: Any) -> Any:
        dispatcher = self

        @functools.wraps(original)
        async def mocked_call_tool(
            inner_self: Any, name: str, arguments: dict[str, Any] | None = None,
            *args: Any, **kwargs: Any,
        ) -> Any:
            return await dispatcher._call(original, inner_self, name, arguments, args, kwargs)

        return mocked_call_tool

    async def _call(
        self, original: Any, inner_self: Any, name: str, arguments: Any,
        args: tuple[Any, ...], kwargs: dict[str, Any],
    ) -> Any:
        match = self._matcher.match(None, name, arguments)
        if match.record is not None:
            self._events.emit(
                "tool_mocked",
                tool_name=name,
                record_id=match.record.get("tool_call_id"),
                how=match.how,
            )
            return _mcp_result_from_record(match.record)
        _report_divergence(self._events, self._policy, ReplayToolUnmatchedError(
            f"{TOOL_SURFACE_MCP}({name!r}) has no unconsumed recorded result: "
            f"{match.reason}; "
            + ("running the live tool" if self._policy == "warn" else "the live tool was not run"),
            tool_name=name,
            arguments_hash=normalized_arg_hash(arguments),
            reason=match.reason,
            surface=TOOL_SURFACE_MCP,
        ))
        self._events.emit("tool_live", tool_name=name)
        return await original(inner_self, name, arguments, *args, **kwargs)


# ── subprocess entry point ───────────────────────────────────────────────────


def install_from_env() -> None:
    """Install the dispatchers described by the engine's environment variables.

    Called from the generated ``sitecustomize.py``. A failure to install under
    the ``fail`` policy stops the process (exit
    ``REPLAY_DISPATCHER_UNAVAILABLE_EXIT``) before the workload runs.
    """
    model_path = os.environ.get("NOVAFABRIC_REPLAY_QUEUE_PATH", "")
    if not model_path:
        return
    events = ReplayEventLog(os.environ.get("NOVAFABRIC_REPLAY_EVENTS_PATH") or None)
    policy = "warn" if os.environ.get("NOVAFABRIC_REPLAY_DIVERGENCE_POLICY") == "warn" else "fail"
    try:
        model_calls = json.loads(Path(model_path).read_text())
        model_dispatcher = MockModelDispatcher(
            model_calls, divergence_policy=policy, events=events
        )
        model_dispatcher.install()
        surfaces = list(model_dispatcher.installed_surfaces)
        tool_path = os.environ.get("NOVAFABRIC_REPLAY_TOOL_QUEUE_PATH", "")
        if tool_path:
            tool_dispatcher = MockToolDispatcher(
                json.loads(Path(tool_path).read_text()),
                divergence_policy=policy,
                events=events,
            )
            tool_dispatcher.install()
            surfaces.extend(tool_dispatcher.installed_surfaces)
        events.emit("installed", policy=policy, surfaces=surfaces)
    except Exception as exc:  # noqa: BLE001 -- reported, then fail-closed below
        events.emit("install_failed", error=f"{type(exc).__name__}: {exc}")
        print(f"[novafabric] mock dispatcher install failed: {exc}", file=sys.stderr)
        if policy != "warn":
            os._exit(REPLAY_DISPATCHER_UNAVAILABLE_EXIT)
