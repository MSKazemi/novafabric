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

**Function tools that were not wrapped** are observed (ADR-0306 r3 amendment).
While capture or a mocked replay is active, :func:`install_observer` patches
the SDK's private helper that every run invokes a ``FunctionTool`` through,
``agents.tool._invoke_function_tool_with_metadata`` (and the copy
``agents.run_internal.tool_execution`` imported by name; checked against
``openai-agents`` 0.24.0 only -- not an SDK extension point; restored by
:func:`uninstall_observer`). A call whose invoker is not a seam copy reaches
the same handler with ``wrapped=False`` and mutation class ``unknown``: capture
records it (marked ``observed``), and mocked replay serves or refuses it like
a wrapped tool declared ``unknown``. The observer steps aside for seam copies
(they ask themselves) and ``record.tool`` functions. Agents used as tools
(``Agent.as_tool``) are observed since ADR-0306 r3c: served when recorded and
servable, their sub-run's calls consumed as covered; one whose sub-run has an
effect beyond its value (``on_stream``, ``hooks``, ``session``, a server-side
conversation, a nested approval) is recorded not servable
(:func:`agent_tool_option_obstacles`, :func:`agent_tool_run_pending`). A
release that renames the helper leaves unwrapped tools unobserved again (they
then run live in a replay).

Nothing here imports ``agents`` at module level: the module is importable, and
the capture hook installable, without it.
"""

from __future__ import annotations

import copy
import functools
import json
import logging
import sys
import threading
from collections.abc import Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
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


@dataclass(frozen=True)
class OaiToolCall(AdkToolCall):
    """One Agents SDK tool call as the seam sees it (ADK's fields, plus ``wrapped``)."""

    #: ``False`` for a tool not passed through :func:`wrap_tools` (seen by the
    #: observer): recorded as ``observed``, gated as ``unknown``.
    wrapped: bool = True
    #: Why serving this ``Agent.as_tool`` call would drop an effect of its
    #: sub-run beyond the returned value (empty for any other tool, ADR-0306 r3c).
    agent_tool_obstacles: tuple[str, ...] = ()
    #: An ``Agent.as_tool`` call (the SDK's ``_is_agent_tool`` marker).
    agent_tool: bool = False


#: How an Agents SDK record was captured (``io.novafabric.openai_agents_tool_seam``).
SEAM_EXT = codec.OPENAI_AGENTS_SEAM_EXT
SEAM_WRAPPED = "wrap_tools"
SEAM_OBSERVED = "observed"
#: The mutation class of a tool that was not wrapped: nothing declared one.
OBSERVED_MUTATION_CLASS = "unknown"


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


#: ``Agent.as_tool`` options whose effect a served call would drop: a callback
#: fed the sub-run's events or lifecycle, a session the sub-run writes, a
#: server-side conversation it continues (``agents/agent.py`` ``as_tool``,
#: openai-agents 0.24.0; read from the closure of the SDK's ``_run_agent_impl``).
_AGENT_TOOL_EFFECT_OPTIONS: tuple[str, ...] = (
    "on_stream", "hooks", "session", "previous_response_id", "conversation_id",
)
_AGENT_TOOL_IMPL = "_run_agent_impl"
AGENT_TOOL_OPTIONS_UNKNOWN = (
    "an Agent.as_tool call whose options could not be read on this openai-agents "
    "version (the SDK's _run_agent_impl closure was not found): serving it could "
    "drop an on_stream / hooks / session effect of its sub-run"
)
AGENT_TOOL_INTERRUPTED = (
    "the Agent.as_tool sub-run paused for approval (nested interruptions) or resumed "
    "one: serving it would drop the paused state the parent run reads"
)


def is_agent_tool(tool: Any) -> bool:
    """An ``Agent.as_tool`` function tool (the SDK's own marker)."""
    return getattr(tool, "_is_agent_tool", False) is True


def _agent_tool_impl(tool: Any) -> Any:
    """The SDK's ``_run_agent_impl`` behind an agent tool's invoker, else ``None``."""
    invoker = getattr(tool, "on_invoke_tool", None)
    for _ in range(3):  # a seam copy wraps the SDK's failure-handling invoker
        if invoker is None:
            return None
        if getattr(invoker, "__name__", None) == _AGENT_TOOL_IMPL:
            return invoker
        nxt = getattr(invoker, "_invoke_tool_impl", None)
        invoker = nxt if nxt is not None else getattr(invoker, "_inner", None)
    return None


def agent_tool_option_obstacles(tool: Any) -> tuple[str, ...]:
    """Why serving this agent tool's call would drop a sub-run effect (static
    options only); ``()`` when nothing does or *tool* is no agent tool. Fails
    closed: options that cannot be read are an obstacle."""
    if not is_agent_tool(tool):
        return ()
    impl = _agent_tool_impl(tool)
    code = getattr(impl, "__code__", None)
    closure = getattr(impl, "__closure__", None)
    if code is None or closure is None:
        return (AGENT_TOOL_OPTIONS_UNKNOWN,)
    cells = dict(zip(code.co_freevars, closure))
    if not all(name in cells for name in _AGENT_TOOL_EFFECT_OPTIONS):
        return (AGENT_TOOL_OPTIONS_UNKNOWN,)
    out = []
    for name in _AGENT_TOOL_EFFECT_OPTIONS:
        try:
            value = cells[name].cell_contents
        except ValueError:  # an empty cell
            return (AGENT_TOOL_OPTIONS_UNKNOWN,)
        if value is not None:
            out.append(
                f"the Agent.as_tool call sets {name}: serving it would drop that "
                "effect of its sub-run"
            )
    return tuple(out)


def agent_tool_run_pending(context: Any) -> bool:
    """Whether the SDK holds a nested run result for this agent-tool call -- a
    sub-run that paused for approval, or one being resumed. Fails closed: ``True``
    when the SDK's private store cannot be read."""
    tool_call = getattr(context, "tool_call", None)
    if tool_call is None:
        return False
    try:
        from agents.agent_tool_state import (
            get_agent_tool_state_scope,
            peek_agent_tool_run_result,
        )

        scope = get_agent_tool_state_scope(context)
        return peek_agent_tool_run_result(tool_call, scope_id=scope) is not None
    except Exception:  # noqa: BLE001 -- an SDK without the store: assume the worst
        return True


def describe_call(
    tool: Any, func: Any, context: Any, arguments_json: Any, mutation_class: str,
    *, wrapped: bool = True,
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
    agent_tool = is_agent_tool(tool)
    obstacles = agent_tool_option_obstacles(tool)
    if agent_tool and agent_tool_run_pending(context):  # a resumed sub-run
        obstacles = (*obstacles, AGENT_TOOL_INTERRUPTED)
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
        wrapped=wrapped,
        agent_tool_obstacles=obstacles,
        agent_tool=agent_tool,
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


# ── observer: function tools that were not wrapped ───────────────────────────

#: Modules holding a reference to the SDK's function-tool invocation helper:
#: ``agents.tool`` defines it, ``agents.run_internal.tool_execution`` imports it
#: by name (``invoke_function_tool``, used by realtime sessions, looks it up in
#: ``agents.tool`` at call time). Checked against openai-agents 0.24.0.
_INVOKE_HELPER = "_invoke_function_tool_with_metadata"
_INVOKE_MODULES = ("agents.tool", "agents.run_internal.tool_execution")
#: Imported last of the two; the post-import hook patches both after it runs.
_TRIGGER_MODULE = "agents.run_internal.tool_execution"
#: Attribute marking the observer's replacement helper.
_OBSERVER_MARKER = "__novafabric_oai_observer__"

_OBSERVER_LOCK = threading.RLock()
_observer_users = 0
#: The helper the observer replaced (one function, bound in several modules).
_observer_original: Any = None


def _observer_skip(tool: Any) -> bool:
    """Whether the observer leaves this call alone (the seam or another surface
    owns it): a seam copy or a ``record.tool`` function. An agent used as a tool
    (``Agent.as_tool``) is observed since ADR-0306 r3c: served when recorded,
    its sub-run's calls consumed as covered."""
    invoker = getattr(tool, "on_invoke_tool", None)
    if getattr(invoker, _MARKER, False):
        return True
    return _skip(_wrapped_callable(tool))


def _observing(original: Any) -> Any:
    """The helper, asking the handler about every unwrapped function-tool call."""

    @functools.wraps(original)
    async def invoke(*args: Any, **kwargs: Any) -> Any:
        handler = _handler
        tool = kwargs.get("function_tool")
        context = kwargs.get("context")
        if handler is None or tool is None or args or _observer_skip(tool):
            return await original(*args, **kwargs)
        call = describe_call(
            tool, _wrapped_callable(tool), context, kwargs.get("arguments"),
            OBSERVED_MUTATION_CLASS, wrapped=False,
        )
        answer = handler.before(call)
        if isinstance(answer, Served):
            # The helper's own result type, built the way it builds a normal one.
            result_cls = getattr(sys.modules.get("agents.tool"), "_FunctionToolInvocationResult")
            return result_cls(answer.value)
        try:
            result = await original(*args, **kwargs)
        except BaseException as exc:
            handler.on_error(context, exc)
            raise
        handler.after(context, getattr(result, "output", result))
        return result

    setattr(invoke, _OBSERVER_MARKER, True)
    return invoke


def _patch_invoke(_module: Any = None) -> None:
    """Replace the helper in every loaded module that holds it (idempotent)."""
    global _observer_original
    with _OBSERVER_LOCK:
        if _observer_users <= 0:
            return
        defining = sys.modules.get(_INVOKE_MODULES[0])
        current = getattr(defining, _INVOKE_HELPER, None)
        if not callable(current):
            _log.warning(
                "novafabric: agents.tool.%s not found; OpenAI Agents function tools "
                "not passed through wrap_tools are not observed (they run live in a "
                "mocked replay)", _INVOKE_HELPER,
            )
            return
        if _observer_original is None:
            original = getattr(current, "__wrapped__", current) if getattr(
                current, _OBSERVER_MARKER, False
            ) else current
            _observer_original = original
        replacement = _observing(_observer_original)
        for name in _INVOKE_MODULES:
            module = sys.modules.get(name)
            if module is None:
                continue
            value = getattr(module, _INVOKE_HELPER, None)
            if value is _observer_original or getattr(value, _OBSERVER_MARKER, False):
                setattr(module, _INVOKE_HELPER, replacement)


def install_observer() -> None:
    """Observe every Agents SDK function-tool call that is not a seam copy.

    Reference-counted (capture and replay may both ask). Never imports
    ``agents``: patched now if the SDK is loaded, otherwise when the workload
    imports it (a one-shot post-import hook).
    """
    global _observer_users
    with _OBSERVER_LOCK:
        _observer_users += 1
        if _observer_users > 1:
            return
    if _TRIGGER_MODULE in sys.modules:
        _patch_invoke()
        return
    from novafabric.capture.hooks._deferred import register_post_import_hook

    register_post_import_hook(_TRIGGER_MODULE, _patch_invoke)


def uninstall_observer() -> None:
    """Drop one :func:`install_observer`; the last one restores the helper."""
    global _observer_users, _observer_original
    with _OBSERVER_LOCK:
        if _observer_users <= 0:
            return
        _observer_users -= 1
        if _observer_users or _observer_original is None:
            return
        for name in _INVOKE_MODULES:
            module = sys.modules.get(name)
            if module is not None and getattr(
                getattr(module, _INVOKE_HELPER, None), _OBSERVER_MARKER, False
            ):
                setattr(module, _INVOKE_HELPER, _observer_original)
        _observer_original = None


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

    def install(self) -> None:
        super().install()
        if self._installed:
            # Function tools the workload never passed through wrap_tools too.
            install_observer()

    def uninstall(self) -> None:
        if self._installed:
            uninstall_observer()
        super().uninstall()

    def build_record(
        self, pending: _base._Pending, nested: int, *,
        value: Any = None, error: BaseException | None = None,
    ) -> dict[str, Any]:
        record = super().build_record(pending, nested, value=value, error=error)
        ext = record["extensions"]
        # Observed records follow the wrapped records' payload and codec rules
        # and are served alike (ADR-0306 r3 amendment).
        ext[SEAM_EXT] = SEAM_WRAPPED if getattr(pending.call, "wrapped", True) else SEAM_OBSERVED
        # ADR-0306 r3c: an Agent.as_tool call is served only when its returned
        # value is all its sub-run did that the workload can see.
        obstacles = list(getattr(pending.call, "agent_tool_obstacles", ()))
        if (
            getattr(pending.call, "agent_tool", False)
            and AGENT_TOOL_INTERRUPTED not in obstacles
            and agent_tool_run_pending(pending.call.tool_context)  # a paused sub-run
        ):
            obstacles.append(AGENT_TOOL_INTERRUPTED)
        if obstacles:
            reasons = [ext.get(codec.NOT_SERVABLE_REASON_EXT), *obstacles]
            ext[codec.RESULT_CODEC_EXT] = codec.CODEC_NOT_SERVABLE
            ext[codec.NOT_SERVABLE_REASON_EXT] = "; ".join(r for r in reasons if r)
        return record
