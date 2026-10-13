"""OpenAI Agents SDK function-tool seam (ADR-0306 slice 3, experimental).

Unlike Google ADK, the Agents SDK documents **no** way to skip a tool.
:func:`wrap_tools` therefore replaces ``FunctionTool.on_invoke_tool`` on a
*copy* of each tool with :class:`_SeamInvoker`. That is an undocumented seam
(``openai-agents`` 0.17.x): it was checked to be reached by ``Runner.run``,
``Runner.run_streamed``, parallel tool calls, ``Agent.clone`` and
``Agent.as_tool`` (ADR-0306 *OpenAI Agents precondition spike*), and
``needs_approval`` still interrupts a run before it. Because it is private SDK
surface, the seam fails safe:

* a tool whose ``on_invoke_tool`` is not callable is returned unchanged;
* the invoker delegates to the SDK's own invoker for every attribute it does
  not define (``prepare_arguments``, the sync marker, ``__wrapped__`` lookups)
  and re-wraps itself when the SDK rebinds it to a copied tool;
* outside capture and replay no handler is registered and the invoker only
  forwards the call.

The registered handler is either the capture sink (:class:`OaiToolHook`, a
built-in capture hook, writing one ``transport: "python"`` record per call with
the ``record.tool`` codec) or the mocked-replay server
(``replay._oai_tool_server``), which answers *before* the tool body runs.

Nothing here imports ``agents`` at module level: the module is importable, and
the capture hook installable, without it.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Iterable, Mapping
from contextvars import ContextVar
from typing import Any

from novafabric.adapters import _adk_tool_seam as _base
from novafabric.adapters._adk_tool_seam import AdkToolCall, AdkToolHandler, AdkToolHook
from novafabric.capture import _tool_codec as codec

_log = logging.getLogger(__name__)

#: The model's ``call_id`` (``ToolContext.tool_call_id``). Feeds the replay
#: matcher's id tier.
CALL_ID_EXT = "io.novafabric.openai_agents_call_id"
#: ``module.qualname`` of the tool class, informational.
TOOL_CLASS_EXT = "io.novafabric.openai_agents_tool_class"

FAILURE_REASON = (
    "the tool raised: a recorded Agents SDK tool failure is not served -- the SDK "
    "either turns it into a model-visible error string (recorded as that string) "
    "or fails the run, and replay cannot reproduce the second faithfully"
)

#: Attribute marking an invoker as already wrapped (``wrap_tools`` is idempotent).
_MARKER = "__novafabric_oai_seam__"


class Served:
    """A replay answer: *value* is returned instead of running the tool.

    A wrapper object because a recorded ``None`` result is a valid answer and
    ``None`` already means "run the tool".
    """

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value


class OaiToolCall(AdkToolCall):
    """One Agents SDK tool call as the seam sees it (same fields as ADK's)."""


#: The one global the invoker reads. ``None`` outside capture and replay.
_handler: AdkToolHandler | None = None


def _set_handler(handler: AdkToolHandler | None) -> AdkToolHandler | None:
    """Install *handler*; return the one it replaced."""
    global _handler
    previous, _handler = _handler, handler
    return previous


def _get_handler() -> AdkToolHandler | None:
    return _handler


def _function_tool_class() -> Any:
    try:
        from agents import FunctionTool
    except ImportError:
        return None
    return FunctionTool


def _wrapped_callable(tool: Any) -> Any:
    """The user's function behind a ``@function_tool``, if the SDK exposes it."""
    try:
        return tool.__wrapped__
    except Exception:  # noqa: BLE001 -- absent, or the invoker was replaced
        return None


def describe_call(
    tool: Any, func: Any, context: Any, arguments_json: Any, mutation_class: str
) -> OaiToolCall:
    """Canonicalise one call (D6): the arguments are the JSON the model sent."""
    arguments: dict[str, Any] | None = None
    not_canonical: str | None = None
    try:
        parsed = json.loads(arguments_json) if arguments_json else {}
        if not isinstance(parsed, dict):
            raise codec.NotJSONRepresentable(
                "the tool arguments", f"are a {type(parsed).__name__}, not an object"
            )
        arguments = copy.deepcopy(codec.json_native(parsed, "the tool arguments"))
    except codec.NotJSONRepresentable as exc:
        not_canonical = f"{exc.where} {exc.why}; it is not JSON-representable"
    except (TypeError, ValueError):
        not_canonical = "the tool arguments are not valid JSON"
    if func is not None and hasattr(func, "__qualname__"):
        qualname = f"{getattr(func, '__module__', 'unknown')}:{func.__qualname__}"
    else:
        qualname = f"{type(tool).__module__}:{type(tool).__qualname__}"
    raw_id = getattr(context, "tool_call_id", None)
    return OaiToolCall(
        tool_name=str(getattr(tool, "name", None) or type(tool).__name__),
        arguments=arguments,
        not_canonical=not_canonical,
        function_call_id=raw_id if isinstance(raw_id, str) and raw_id else None,
        mutation_class=mutation_class,
        tool_class=f"{type(tool).__module__}.{type(tool).__qualname__}",
        qualname=qualname,
        long_running=False,
        tool_context=context,
    )


def _skip(func: Any) -> bool:
    """Step aside for a function already declared with ``record.tool``."""
    return func is not None and getattr(func, "__novafabric_tool__", None) is not None


class _SeamInvoker:
    """``FunctionTool.on_invoke_tool`` replacement that consults the handler."""

    def __init__(self, inner: Any, config: _Config, tool: Any, func: Any) -> None:
        self._inner = inner
        self._config = config
        self._tool = tool
        #: The user's function, read from the tool *before* its invoker was
        #: replaced (afterwards the SDK's ``__wrapped__`` raises).
        self._func = func
        setattr(self, _MARKER, True)

    def __getattr__(self, name: str) -> Any:
        # Only reached for names this class lacks: ``prepare_arguments`` (approval
        # preparation), ``__agents_sync_function_tool__``, ...
        if name.startswith("__") and name.endswith("__") and name not in (
            "__agents_sync_function_tool__",
        ):
            raise AttributeError(name)
        return getattr(self._inner, name)

    def __agents_bind_function_tool__(self, function_tool: Any) -> Any:
        """The SDK rebinds the invoker when it copies a tool; keep the seam."""
        bind = getattr(self._inner, "__agents_bind_function_tool__", None)
        inner = bind(function_tool) if callable(bind) else self._inner
        if inner is self._inner:
            self._tool = function_tool
            return self
        return _SeamInvoker(inner, self._config, function_tool, self._func)

    async def __call__(self, context: Any, arguments_json: Any) -> Any:
        handler = _handler
        if handler is None or _skip(self._func):
            return await self._inner(context, arguments_json)
        call = describe_call(
            self._tool, self._func, context, arguments_json,
            self._config.mutation_class_for(str(getattr(self._tool, "name", "") or "")),
        )
        answer = handler.before(call)
        if isinstance(answer, Served):
            return answer.value
        try:
            result = await self._inner(context, arguments_json)
        except BaseException as exc:
            handler.on_error(context, exc)
            raise
        handler.after(context, result)
        return result


class _Config:
    def __init__(
        self, mutation_classes: Mapping[str, str] | None, default_mutation_class: str
    ) -> None:
        self.classes = dict(mutation_classes or {})
        for tool_name, value in [*self.classes.items(), ("<default>", default_mutation_class)]:
            if value not in codec.MUTATION_CLASSES:
                raise ValueError(
                    f"mutation class for {tool_name!r} must be one of "
                    f"{codec.MUTATION_CLASSES}, got {value!r}"
                )
        self.default = default_mutation_class

    def mutation_class_for(self, tool_name: str) -> str:
        return self.classes.get(tool_name, self.default)


def wrap_tools(
    tools: Iterable[Any],
    mutation_classes: Mapping[str, str] | None = None,
    default_mutation_class: str = "unknown",
) -> list[Any]:
    """Return *tools* with every ``FunctionTool`` routed through the seam.

    Each wrapped tool is a **copy**; the originals are not touched. Tools that
    are not ``FunctionTool`` instances (hosted tools, MCP servers, ...), tools
    already wrapped, and tools whose ``on_invoke_tool`` is not callable are
    returned as they are, and keep running live in a replay.

    ``mutation_classes`` maps a tool name to one of ``codec.MUTATION_CLASSES``
    (declared in the workload's own code, never read from a capsule); the
    default is ``"unknown"``, the most restrictive class.
    """
    config = _Config(mutation_classes, default_mutation_class)
    function_tool = _function_tool_class()
    out: list[Any] = []
    for tool in tools:
        invoker = getattr(tool, "on_invoke_tool", None)
        if (
            function_tool is None
            or not isinstance(tool, function_tool)
            or not callable(invoker)
            or getattr(invoker, _MARKER, False)
        ):
            out.append(tool)
            continue
        wrapped = copy.copy(tool)
        func = _wrapped_callable(wrapped)
        wrapped.on_invoke_tool = _SeamInvoker(wrapped.on_invoke_tool, config, wrapped, func)
        out.append(wrapped)
    return out


# ── capture ──────────────────────────────────────────────────────────────────

#: The call in flight in this context. A separate variable from ADK's, so an
#: ADK tool nested in an Agents tool (or the reverse) never sees the other's entry.
_pending: ContextVar[_base._Pending | None] = ContextVar(
    "novafabric_oai_tool_pending", default=None
)


class OaiToolHook(AdkToolHook):
    """Capture sink for Agents SDK tool calls; a built-in hook keyed on ``agents``.

    Installing it imports nothing from the SDK. It never displaces a replay
    server (see :class:`AdkToolHook`).
    """

    SURFACE_MARKER = codec.TOOL_SURFACE_OPENAI_AGENTS_TOOL
    PROVIDER_SCHEME = "openai-agents"
    CALL_ID_EXT = CALL_ID_EXT
    CLASS_EXT = TOOL_CLASS_EXT
    SUBJECT = "Agents SDK tool"
    FAILURE_REASON = FAILURE_REASON

    _get_handler = staticmethod(lambda: _get_handler())
    _set_handler = staticmethod(lambda handler: _set_handler(handler))
    _pending_var = staticmethod(lambda: _pending)
