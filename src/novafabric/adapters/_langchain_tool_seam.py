"""LangChain / LangGraph tool seam (ADR-0306 slice 3, experimental).

LangChain documents no hook that can *answer* a tool call for every caller:
callbacks (``on_tool_start``) only observe, and LangGraph's
``ToolNode(wrap_tool_call=...)`` short-circuit covers ``ToolNode`` only, must
build the ``ToolMessage`` itself, and is a single slot a workload may already
use. :func:`wrap_tools` instead returns, for each ``BaseTool``, a **copy** whose
class is a generated subclass of the tool's own class that overrides:

* the public ``run`` / ``arun`` -- to note the raw input (the arguments the
  model sent) and the model's ``tool_call_id`` (``ToolCall["id"]``) for this
  call, in a context variable; then it delegates unchanged;
* the body hooks ``_run`` / ``_arun`` -- the methods LangChain documents for
  implementing a tool -- to ask the registered handler *before* the parent's
  body runs. A served answer is returned as the body's result, so LangChain
  still validates the input, fires the callbacks, applies ``response_format``
  and builds the ``ToolMessage`` exactly as for a live call.

The overrides carry the parent's signature and annotations
(``functools.wraps``), because LangChain inspects ``_run`` / ``_arun`` for
``run_manager``, a ``RunnableConfig`` parameter and injected arguments. It was
checked against ``langchain-core`` 1.8.3 and ``langgraph`` 1.2.23
(``langgraph-prebuilt`` 1.1.3): ``invoke`` / ``ainvoke`` with a plain dict or a
``ToolCall``, ``@tool`` sync and async functions, ``StructuredTool``, a
``BaseTool`` subclass, and ``ToolNode`` run sync and async inside a
``StateGraph`` (parallel calls, ``InjectedState`` / ``InjectedToolCallId``).
The generated class and the ``__class__`` swap are NovaFabric's construction,
not a LangChain API; a release that changes how ``run`` reaches the body can
bypass the seam (the tool then runs live in a replay).

Outside capture and replay no handler is registered and the copy behaves like
the original. Nothing here imports ``langchain_core`` at module level: the
module is importable, and the capture hook installable, without it.
"""

from __future__ import annotations

import copy
import functools
import importlib
import logging
import threading
from collections.abc import Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from novafabric.adapters import _adk_tool_seam as _base
from novafabric.adapters._adk_tool_seam import AdkToolCall, AdkToolHandler, AdkToolHook
from novafabric.adapters._oai_tool_seam import Served
from novafabric.capture import _tool_codec as codec

__all__ = ["CALL_ID_EXT", "LangChainToolCall", "LangChainToolHook", "Served", "wrap_tools"]

_log = logging.getLogger(__name__)

#: The model's tool-call id (``ToolCall["id"]``, the ``tool_call_id`` LangChain
#: passes to ``BaseTool.run``). Feeds the replay matcher's id tier.
CALL_ID_EXT = "io.novafabric.langchain_tool_call_id"
#: ``module.qualname`` of the tool class, informational.
TOOL_CLASS_EXT = "io.novafabric.langchain_tool_class"

FAILURE_REASON = (
    "the tool raised: a recorded LangChain tool failure is not served -- LangChain "
    "either turns it into a model-visible error message (handle_tool_error / "
    "ToolNode handle_tool_errors) or fails the run, and replay cannot reproduce the "
    "choice faithfully from the seam"
)

#: Class attribute marking a generated seam class; its value is the original class.
_ORIGINAL = "__novafabric_langchain_original__"


@dataclass(frozen=True)
class LangChainToolCall(AdkToolCall):
    """One LangChain tool call as the seam sees it, before the body runs."""

    #: Input keys left out of ``arguments`` (injected state, store, runtime,
    #: ``InjectedToolCallId``, ...): the model never sent them.
    ignored: tuple[str, ...] = ()


#: The one global the seam reads. ``None`` outside capture and replay.
_handler: AdkToolHandler | None = None


def _set_handler(handler: AdkToolHandler | None) -> AdkToolHandler | None:
    """Install *handler*; return the one it replaced."""
    global _handler
    previous, _handler = _handler, handler
    return previous


def _get_handler() -> AdkToolHandler | None:
    return _handler


class _Current:
    """The input of one ``run`` / ``arun`` call, noted for its body hook.

    Also the ``tool_context`` the handler sees: the capture sink pairs
    ``before`` with ``after`` by identity. No ``actions`` attribute, so the
    shared sink never reports a changed ``tool_context.actions``.
    """

    __slots__ = ("tool", "tool_input", "tool_call_id", "claimed")

    def __init__(self, tool: Any, tool_input: Any, tool_call_id: Any) -> None:
        self.tool = tool
        self.tool_input = tool_input
        self.tool_call_id = tool_call_id
        #: Set by the first body hook that takes the call; a body that delegates
        #: to another hooked body (``StructuredTool._arun`` -> ``BaseTool._arun``
        #: -> ``_run`` in an executor) must not ask the handler twice.
        self.claimed = False


_current: ContextVar[_Current | None] = ContextVar("novafabric_langchain_tool_call", default=None)


def _model_fields(tool: Any) -> list[str] | None:
    """The argument names the model sees (``tool_call_schema``), if known."""
    try:
        schema = tool.tool_call_schema
        if isinstance(schema, dict):
            props = schema.get("properties")
            return list(props) if isinstance(props, dict) else None
        fields = getattr(schema, "model_fields", None)
        if isinstance(fields, dict):
            return list(fields)
    except Exception:  # noqa: BLE001 -- an odd schema only loses the filter
        _log.debug("langchain: could not read tool_call_schema", exc_info=True)
    return None


def _user_function(tool: Any) -> Any:
    for name in ("func", "coroutine"):
        value = getattr(tool, name, None)
        if callable(value):
            return value
    return None


def original_class(tool: Any) -> type:
    cls = type(tool)
    original = cls.__dict__.get(_ORIGINAL)
    return original if isinstance(original, type) else cls


def describe_call(current: _Current, mutation_class: str) -> LangChainToolCall:
    """Canonicalise one call (D6): the arguments are the ones the model sent."""
    tool, tool_input = current.tool, current.tool_input
    arguments: dict[str, Any] | None = None
    not_canonical: str | None = None
    ignored: tuple[str, ...] = ()
    fields = _model_fields(tool)
    try:
        if isinstance(tool_input, str):
            # LangChain binds a string input to the first argument (`_parse_input`).
            if not fields:
                raise codec.NotJSONRepresentable(
                    "the tool input", "is a string, and the tool declares no argument"
                )
            raw: dict[str, Any] = {fields[0]: tool_input}
        elif isinstance(tool_input, dict):
            raw = dict(tool_input)
        else:
            raise codec.NotJSONRepresentable(
                "the tool input", f"is a {type(tool_input).__name__}, not a dict"
            )
        if fields is not None:
            ignored = tuple(sorted(k for k in raw if k not in fields))
            raw = {k: v for k, v in raw.items() if k in fields}
        # A copy: the tool receives (and may mutate) the original.
        arguments = copy.deepcopy(codec.json_native(raw, "the tool arguments"))
    except codec.NotJSONRepresentable as exc:
        not_canonical = f"{exc.where} {exc.why}; it is not JSON-representable"
    except Exception as exc:  # noqa: BLE001 -- an odd argument object is a refusal
        not_canonical = f"the tool arguments could not be copied: {type(exc).__name__}"
    func = _user_function(tool)
    cls = original_class(tool)
    if func is not None and hasattr(func, "__qualname__"):
        qualname = f"{getattr(func, '__module__', 'unknown')}:{func.__qualname__}"
    else:
        qualname = f"{cls.__module__}:{cls.__qualname__}"
    raw_id = current.tool_call_id
    return LangChainToolCall(
        tool_name=str(getattr(tool, "name", None) or cls.__name__),
        arguments=arguments,
        not_canonical=not_canonical,
        function_call_id=raw_id if isinstance(raw_id, str) and raw_id else None,
        mutation_class=mutation_class,
        tool_class=f"{cls.__module__}.{cls.__qualname__}",
        qualname=qualname,
        long_running=False,
        tool_context=current,
        ignored=ignored,
    )


def skip_reason(tool: Any) -> str | None:
    """Why the seam steps aside for *tool* (records nothing, serves nothing).

    * a tool built by ``langchain_mcp_adapters`` calls
      ``mcp.ClientSession.call_tool``, already an intercepted surface
      (ADR-0300); a boundary here would make every MCP call nested and
      unservable;
    * a function declared with ``record.tool`` is a boundary of its own.
    """
    func = _user_function(tool)
    if func is None:
        return None
    if str(getattr(func, "__module__", "") or "").startswith("langchain_mcp_adapters"):
        return "mcp"
    if getattr(func, "__novafabric_tool__", None) is not None:
        return "record.tool"
    return None


def _take(tool: Any) -> _Current | None:
    """The noted call this body hook should hand to the handler, if any."""
    current = _current.get()
    if current is None or current.tool is not tool or current.claimed:
        return None
    current.claimed = True
    return current


def _seam_class(cls: Any, mutation_class: str, base_tool: Any) -> type:
    """The generated subclass of *cls* declaring *mutation_class* (cached per pair).

    *base_tool* is ``langchain_core.tools.BaseTool``. The generated class
    derives from *cls* alone, so ``cls.run`` is exactly what ``super()`` would
    reach.
    """
    key = (cls, mutation_class)
    with _CLASSES_LOCK:
        cached = _CLASSES.get(key)
        if cached is not None:
            return cached
    parent_public_run = cls.run
    parent_public_arun = cls.arun

    def run(self: Any, tool_input: Any, *args: Any, **kwargs: Any) -> Any:
        token = _current.set(_Current(self, tool_input, kwargs.get("tool_call_id")))
        try:
            return parent_public_run(self, tool_input, *args, **kwargs)
        finally:
            _current.reset(token)

    async def arun(self: Any, tool_input: Any, *args: Any, **kwargs: Any) -> Any:
        token = _current.set(_Current(self, tool_input, kwargs.get("tool_call_id")))
        try:
            return await parent_public_arun(self, tool_input, *args, **kwargs)
        finally:
            _current.reset(token)

    parent_run = cls._run

    @functools.wraps(parent_run)
    def _run(self: Any, *args: Any, **kwargs: Any) -> Any:
        handler = _handler
        current = _take(self) if handler is not None else None
        if handler is None or current is None:
            return parent_run(self, *args, **kwargs)
        answer = handler.before(describe_call(current, mutation_class))
        if isinstance(answer, Served):
            return answer.value
        try:
            result = parent_run(self, *args, **kwargs)
        except BaseException as exc:
            handler.on_error(current, exc)
            raise
        handler.after(current, result)
        return result

    namespace: dict[str, Any] = {
        "__module__": cls.__module__,
        "__qualname__": cls.__qualname__,
        _ORIGINAL: cls,
        "run": run,
        "arun": arun,
        "_run": _run,
    }
    # LangChain checks `cls._arun is BaseTool._arun` to decide whose signature to
    # inspect; keep that answer unchanged by overriding `_arun` only when the
    # tool class does.
    if cls._arun is not base_tool._arun:
        parent_arun = cls._arun

        @functools.wraps(parent_arun)
        async def _arun(self: Any, *args: Any, **kwargs: Any) -> Any:
            handler = _handler
            current = _take(self) if handler is not None else None
            if handler is None or current is None:
                return await parent_arun(self, *args, **kwargs)
            answer = handler.before(describe_call(current, mutation_class))
            if isinstance(answer, Served):
                return answer.value
            try:
                result = await parent_arun(self, *args, **kwargs)
            except BaseException as exc:
                handler.on_error(current, exc)
                raise
            handler.after(current, result)
            return result

        namespace["_arun"] = _arun
    seam = type(cls.__name__, (cls,), namespace)
    with _CLASSES_LOCK:
        return _CLASSES.setdefault(key, seam)


#: Generated seam classes, keyed by (tool class, declared mutation class): at most
#: five per tool class, however often ``wrap_tools`` is called.
_CLASSES: dict[tuple[type, str], type] = {}
_CLASSES_LOCK = threading.Lock()


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

    def mutation_class_for(self, tool_name: Any) -> str:
        return self.classes.get(str(tool_name or ""), self.default)


def _base_tool_class() -> Any:
    """``langchain_core.tools.BaseTool``, or ``None`` when LangChain is absent.

    Imported by name so that type-checking NovaFabric never needs LangChain.
    """
    try:
        return importlib.import_module("langchain_core.tools").BaseTool
    except (ImportError, AttributeError):
        return None


def wrap_tools(
    tools: Iterable[Any],
    mutation_classes: Mapping[str, str] | None = None,
    default_mutation_class: str = "unknown",
) -> list[Any]:
    """Return *tools* with every LangChain ``BaseTool`` routed through the seam.

    Each wrapped tool is a **copy** (``model_copy``) whose class is a generated
    subclass of its own; the originals are not touched. Items that are not
    ``BaseTool`` instances (plain functions, dicts), tools already wrapped,
    MCP tools from ``langchain_mcp_adapters``, ``record.tool`` functions, and
    tools whose class cannot be subclassed are returned as they are and keep
    running live in a replay.

    ``mutation_classes`` maps a tool name to one of ``codec.MUTATION_CLASSES``
    (declared in the workload's own code, never read from a capsule); the
    default is ``"unknown"``, the most restrictive class.
    """
    config = _Config(mutation_classes, default_mutation_class)
    base_tool = _base_tool_class()
    out: list[Any] = []
    for tool in tools:
        if (
            base_tool is None
            or not isinstance(tool, base_tool)
            or _ORIGINAL in type(tool).__dict__
            or skip_reason(tool) is not None
        ):
            out.append(tool)
            continue
        try:
            seam = _seam_class(type(tool), config.mutation_class_for(tool.name), base_tool)
            wrapped = tool.model_copy()
            object.__setattr__(wrapped, "__class__", seam)
        except Exception:  # noqa: BLE001 -- fail safe: the tool stays unwrapped
            _log.warning(
                "novafabric: LangChain tool %r could not be wrapped; it runs live in "
                "a replay", getattr(tool, "name", tool), exc_info=True,
            )
            out.append(tool)
            continue
        out.append(wrapped)
    return out


# ── capture ──────────────────────────────────────────────────────────────────

#: The call in flight in this context; separate from the ADK and Agents SDK
#: variables, so a tool of one framework nested in another's never sees it.
_pending: ContextVar[_base._Pending | None] = ContextVar(
    "novafabric_langchain_tool_pending", default=None
)


class LangChainToolHook(AdkToolHook):
    """Capture sink for LangChain tool calls; a built-in hook keyed on
    ``langchain_core``.

    Installing it imports nothing from LangChain. It never displaces a replay
    server (see :class:`AdkToolHook`).
    """

    SURFACE_MARKER = codec.TOOL_SURFACE_LANGCHAIN_TOOL
    PROVIDER_SCHEME = "langchain"
    CALL_ID_EXT = CALL_ID_EXT
    CLASS_EXT = TOOL_CLASS_EXT
    SUBJECT = "LangChain tool"
    FAILURE_REASON = FAILURE_REASON

    _get_handler = staticmethod(lambda: _get_handler())
    _set_handler = staticmethod(lambda handler: _set_handler(handler))
    _pending_var = staticmethod(lambda: _pending)

    def build_record(
        self, pending: _base._Pending, nested: int, *,
        value: Any = None, error: BaseException | None = None,
    ) -> dict[str, Any]:
        record = super().build_record(pending, nested, value=value, error=error)
        ignored = getattr(pending.call, "ignored", ())
        if ignored:
            record["extensions"][codec.IGNORED_ARGUMENTS_EXT] = list(ignored)
        return record
