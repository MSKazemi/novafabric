"""Google ADK tool seam (ADR-0306 slice 3, experimental).

ADK documents a way to skip a tool: when a plugin's ``before_tool_callback``
returns a dict, "it will stop the tool execution and return this response
immediately" (``google/adk/plugins/base_plugin.py:297-319`` in google-adk
2.11.0; the call site is ``flows/llm_flows/tools/_caller.py:773-777``, where a
non-``None`` plugin answer skips the agent's own callbacks, the confirmation
gate and the tool). :class:`AdkToolSeamPlugin` hooks that seam and delegates
every tool call to whichever handler is registered here:

* **no handler** (outside capture and replay) -- the callbacks return ``None``
  and ADK runs the tool as if the plugin were absent;
* **capture** -- :class:`AdkToolHook`, a built-in capture hook, installed
  when ``google.adk`` is importable, writes one ``tool-calls.jsonl`` record per
  tool call (``transport: "python"``, marker ``io.novafabric.tool_surface:
  "google.adk.tool"``), with the ``record.tool`` codec: canonical JSON
  arguments, after-redaction digests, payloads only at the ``forensic`` /
  ``air_gapped`` capture level (``_tool_codec``), nested-record marking
  (``_tool_scope``);
* **mocked replay** -- ``replay._adk_tool_server.AdkToolServer``, registered by
  ``MockToolDispatcher.install``, answers the call from the capsule *before*
  the tool body runs, or refuses it (fail closed).

Nothing here imports ``google.adk``: the module is importable, and the capture
hook installable, without it.
"""

from __future__ import annotations

import copy
import logging
import time
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

from novafabric.capture import _tool_codec as codec
from novafabric.capture import _tool_scope
from novafabric.capture._ulid import new_ulid
from novafabric.capture.event_recorder import get_current_writer

if TYPE_CHECKING:
    from novafabric.capture.capsule import CapsuleWriter

_log = logging.getLogger(__name__)

#: The model's ``FunctionCall.id`` (``ToolContext.function_call_id``). Feeds the
#: replay matcher's id tier (ADR-0306 slice 3).
FUNCTION_CALL_ID_EXT = "io.novafabric.adk_function_call_id"
#: ``module.qualname`` of the ADK tool class, informational.
TOOL_CLASS_EXT = "io.novafabric.adk_tool_class"

#: Why a recorded ADK tool failure is never served.
ERROR_NOT_SERVED_REASON = (
    "the tool raised: a recorded ADK tool failure is not served -- ADK wraps an "
    "exception raised by a plugin callback in RuntimeError and skips the "
    "on_tool_error callbacks, so it cannot be replayed faithfully through the "
    "plugin seam"
)
LONG_RUNNING_REASON = (
    "long-running or deferred-response ADK tools are not served (their response "
    "arrives later, outside the call)"
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class AdkToolCall:
    """One ADK tool call as the seam sees it, before the tool body runs."""

    tool_name: str
    #: Canonical (JSON-native) arguments, or ``None`` when ``not_canonical``.
    arguments: dict[str, Any] | None
    not_canonical: str | None
    function_call_id: str | None
    #: Declared in the workload's own code (the plugin), never read from a capsule.
    mutation_class: str
    tool_class: str
    qualname: str
    long_running: bool
    tool_context: Any

    @property
    def digest(self) -> str | None:
        """Matching digest, taken **after** secret redaction (ADR-0306 D12.3)."""
        if self.arguments is None:
            return None
        return codec.redacted_arguments_digest(self.arguments)


class AdkToolHandler(Protocol):
    """A capture sink or a replay server for ADK tool calls (internal)."""

    def before(self, call: AdkToolCall) -> Any:
        """A non-``None`` value answers the call; ``None`` runs the tool."""
        ...

    def after(self, tool_context: Any, result: Any) -> None: ...

    def on_error(self, tool_context: Any, error: BaseException) -> None: ...


#: The one global the plugin reads. ``None`` outside capture and replay.
_handler: AdkToolHandler | None = None


def _set_handler(handler: AdkToolHandler | None) -> AdkToolHandler | None:
    """Install *handler*; return the one it replaced."""
    global _handler
    previous, _handler = _handler, handler
    return previous


def _get_handler() -> AdkToolHandler | None:
    return _handler


def _class_name(obj: Any) -> str:
    cls = type(obj)
    return f"{cls.__module__}.{cls.__qualname__}"


def skip_reason(tool: Any) -> str | None:
    """Why the seam steps aside for *tool* (records nothing, serves nothing).

    * an ADK ``McpTool`` calls ``mcp.ClientSession.call_tool``, which is already
      an intercepted surface (ADR-0300) -- taking the call here too would turn
      every MCP call into a nested, unservable boundary;
    * a ``FunctionTool`` whose function is declared with ``record.tool`` is
      already a boundary of its own (ADR-0306 slice 1).
    """
    for cls in type(tool).__mro__:
        if cls.__name__ == "McpTool" and cls.__module__.startswith("google.adk."):
            return "mcp"
    func = getattr(tool, "func", None)
    if func is not None and getattr(func, "__novafabric_tool__", None) is not None:
        return "record.tool"
    return None


def describe_call(
    tool: Any, tool_args: Any, tool_context: Any, mutation_class: str
) -> AdkToolCall:
    """Canonicalise one call (D6): the arguments are the JSON the model sent."""
    arguments: dict[str, Any] | None = None
    not_canonical: str | None = None
    try:
        if not isinstance(tool_args, dict):
            raise codec.NotJSONRepresentable(
                "the tool arguments", f"are a {type(tool_args).__name__}, not a dict"
            )
        # A copy: the tool receives (and may mutate) the original.
        arguments = copy.deepcopy(codec.json_native(tool_args, "the tool arguments"))
    except codec.NotJSONRepresentable as exc:
        not_canonical = f"{exc.where} {exc.why}; it is not JSON-representable"
    except Exception as exc:  # noqa: BLE001 -- an odd argument object is a refusal
        not_canonical = f"the tool arguments could not be copied: {type(exc).__name__}"
    func = getattr(tool, "func", None)
    if func is not None and hasattr(func, "__qualname__"):
        qualname = f"{getattr(func, '__module__', 'unknown')}:{func.__qualname__}"
    else:
        qualname = f"{type(tool).__module__}:{type(tool).__qualname__}"
    raw_id = getattr(tool_context, "function_call_id", None)
    return AdkToolCall(
        tool_name=str(getattr(tool, "name", None) or type(tool).__name__),
        arguments=arguments,
        not_canonical=not_canonical,
        function_call_id=raw_id if isinstance(raw_id, str) and raw_id else None,
        mutation_class=mutation_class,
        tool_class=_class_name(tool),
        qualname=qualname,
        long_running=bool(
            getattr(tool, "is_long_running", False) or getattr(tool, "_defers_response", False)
        ),
        tool_context=tool_context,
    )


class AdkToolSeamPlugin:
    """The ADK tool callbacks, delegating to the registered handler.

    Not a ``BasePlugin`` subclass, so this module imports without
    ``google-adk``; :func:`novafabric.adapters.google_adk.make_tool_plugin`
    mixes it onto ``BasePlugin``. ADK stops at the first plugin that answers a
    callback, so this plugin must come **first** in ``Runner(plugins=[...])``.
    """

    name = "novafabric_tools"

    def __init__(
        self,
        mutation_classes: Mapping[str, str] | None = None,
        default_mutation_class: str = "unknown",
    ) -> None:
        classes = dict(mutation_classes or {})
        for tool_name, value in [*classes.items(), ("<default>", default_mutation_class)]:
            if value not in codec.MUTATION_CLASSES:
                raise ValueError(
                    f"mutation class for {tool_name!r} must be one of "
                    f"{codec.MUTATION_CLASSES}, got {value!r}"
                )
        self._classes = classes
        self._default_class = default_mutation_class

    def mutation_class_for(self, tool_name: str) -> str:
        return self._classes.get(tool_name, self._default_class)

    async def before_tool_callback(
        self, *, tool: Any, tool_args: Any, tool_context: Any, **_: Any
    ) -> Any:
        handler = _handler
        if handler is None or skip_reason(tool) is not None:
            return None
        call = describe_call(
            tool, tool_args, tool_context,
            self.mutation_class_for(str(getattr(tool, "name", "") or "")),
        )
        return handler.before(call)

    async def after_tool_callback(
        self, *, tool: Any, tool_args: Any, tool_context: Any, result: Any, **_: Any
    ) -> None:
        handler = _handler
        if handler is not None:
            handler.after(tool_context, result)
        return None

    async def on_tool_error_callback(
        self, *, tool: Any, tool_args: Any, tool_context: Any, error: BaseException, **_: Any
    ) -> None:
        handler = _handler
        if handler is not None:
            handler.on_error(tool_context, error)
        return None


# ── capture ──────────────────────────────────────────────────────────────────


def _actions_snapshot(tool_context: Any) -> dict[str, Any] | None:
    """Shallow copy of every ``EventActions`` field, to see what the tool changed."""
    try:
        actions = getattr(tool_context, "actions", None)
        fields = getattr(type(actions), "model_fields", None)
        if actions is None or not fields:
            return None
        return {name: copy.copy(getattr(actions, name, None)) for name in fields}
    except Exception:  # noqa: BLE001 -- capture must never block the workload
        return None


def _changed_actions(tool_context: Any, before: dict[str, Any] | None) -> list[str]:
    if before is None:
        return []
    after = _actions_snapshot(tool_context)
    if after is None:
        return ["actions"]
    changed = []
    for name, value in after.items():
        try:
            same = bool(before.get(name) == value)
        except Exception:  # noqa: BLE001 -- incomparable: assume it changed
            same = False
        if not same:
            changed.append(name)
    return changed


class _Pending:
    """One ADK tool call between ``before`` and ``after``/``on_error``."""

    __slots__ = (
        "call", "tool_call_id", "started", "t0", "actions", "boundary",
        "scope_token", "pending_token",
    )

    def __init__(self, call: AdkToolCall) -> None:
        self.call = call
        self.tool_call_id = new_ulid()
        self.started = _now()
        self.t0 = time.monotonic()
        self.actions = _actions_snapshot(call.tool_context)
        self.boundary, self.scope_token = _tool_scope.enter(self.tool_call_id)
        self.pending_token: Token[_Pending | None] | None = None

    def close(self) -> int:
        _tool_scope.leave(self.scope_token)
        if self.pending_token is not None:
            try:
                _pending.reset(self.pending_token)
            except ValueError:  # reset from another context
                _pending.set(None)
        return self.boundary.nested


#: The call in flight in this context. ADK runs a call's before-callbacks, the
#: tool and its after-callbacks in one context, a copy per call (``_caller.py``
#: ``_PreparedFunctionCall.contextvars_snapshot``), so concurrent calls never
#: see each other's entry.
_pending: ContextVar[_Pending | None] = ContextVar("novafabric_adk_tool_pending", default=None)


class AdkToolHook:
    """Capture sink for ADK tool calls; a built-in hook keyed on ``google.adk``.

    Installing it imports nothing from ADK. It never displaces a replay server:
    an in-process capture started *inside* a mocked replay (an adapter plugin)
    must not turn served tools back into live ones.
    """

    def __init__(self, writer: CapsuleWriter, parent_span_id: str) -> None:
        self._writer = writer
        self._parent_span_id = parent_span_id
        self._previous: AdkToolHandler | None = None
        self._installed = False

    def install(self) -> None:
        current = _get_handler()
        if getattr(current, "serves_replay", False):
            _log.debug("google_adk: a replay server is registered; capture sink not installed")
            return
        self._previous = _set_handler(self)
        self._installed = True

    def uninstall(self) -> None:
        if not self._installed:
            return
        if _get_handler() is self:
            _set_handler(self._previous)
        self._previous = None
        self._installed = False

    # ── handler protocol ────────────────────────────────────────────────────

    def before(self, call: AdkToolCall) -> None:
        try:
            pending = _Pending(call)
            pending.pending_token = _pending.set(pending)
        except Exception:  # noqa: BLE001 -- capture must never block the workload
            _log.debug("google_adk: could not start a tool record", exc_info=True)
        return None

    def after(self, tool_context: Any, result: Any) -> None:
        self._finish(tool_context, value=result)

    def on_error(self, tool_context: Any, error: BaseException) -> None:
        self._finish(tool_context, error=error)

    def _finish(
        self, tool_context: Any, *, value: Any = None, error: BaseException | None = None
    ) -> None:
        try:
            pending = _pending.get()
            # A call this plugin never saw start (another plugin answered it
            # first) has no entry, and one already finished has none either.
            if pending is None or pending.call.tool_context is not tool_context:
                return
            nested = pending.close()
            record = self.build_record(pending, nested, value=value, error=error)
            get_current_writer(self._writer).append_tool_call(record)
        except Exception:  # noqa: BLE001 -- recording never raises into the workload
            _log.debug("google_adk: could not write a tool record", exc_info=True)

    def build_record(
        self, pending: _Pending, nested: int, *,
        value: Any = None, error: BaseException | None = None,
    ) -> dict[str, Any]:
        from novafabric.capture.hooks._python_tool import PAYLOADS_OFF_REASON
        from novafabric.capture.record import _payloads_enabled

        call = pending.call
        payloads = _payloads_enabled()
        ext: dict[str, Any] = {
            codec.TOOL_SURFACE_EXT: codec.TOOL_SURFACE_ADK_TOOL,
            codec.QUALNAME_EXT: call.qualname,
            TOOL_CLASS_EXT: call.tool_class,
        }
        if call.function_call_id:
            ext[FUNCTION_CALL_ID_EXT] = call.function_call_id
        if nested:
            ext[codec.NESTED_RECORDS_EXT] = nested

        reasons: list[str] = []
        arguments: dict[str, Any] = {}
        if call.not_canonical is not None:
            reasons.append(call.not_canonical)
        elif payloads:
            arguments = call.arguments or {}
        else:
            ext[codec.ARGUMENTS_DIGEST_EXT] = call.digest
        if not payloads:
            reasons.append(PAYLOADS_OFF_REASON)
        if nested:
            reasons.append(
                f"{nested} model/tool record(s) were written inside this ADK tool call; "
                "serving it would leave them unrequested (nested-call coverage is not "
                "implemented)"
            )
        if call.long_running:
            reasons.append(LONG_RUNNING_REASON)
        changed = _changed_actions(call.tool_context, pending.actions)
        if changed:
            reasons.append(
                f"the call changed tool_context.actions ({', '.join(changed)}); serving "
                "its result would drop that effect"
            )

        record: dict[str, Any] = {
            "schema_version": "0.1.0",
            "tool_call_id": pending.tool_call_id,
            "parent_span_id": self._parent_span_id,
            "started_at": pending.started,
            "finished_at": _now(),
            "duration_ms": max(0, int((time.monotonic() - pending.t0) * 1000)),
            "tool_name": call.tool_name,
            "tool_version": "unknown",
            "tool_provider": f"google-adk://{call.tool_class}",
            "transport": "python",
            "mutates": call.mutation_class not in ("none", "read-only"),
            "mutation_class": call.mutation_class,
            "arguments": arguments,
            "arguments_schema_ref": None,
            "result": None,
            "result_schema_ref": None,
            "status": "success",
            "agent_call_id": None,
        }
        if error is not None:
            record["status"] = "error"
            record["error"] = {
                "type": type(error).__name__,
                "message": str(error) if payloads else "(not recorded at this capture level)",
                "traceback_ref": None,
            }
            if type(error).__module__ == "builtins":
                ext[codec.EXCEPTION_BUILTIN_EXT] = True
            reasons.append(ERROR_NOT_SERVED_REASON)
        else:
            encoded = codec.encode_result(value, keep_value=payloads)
            if encoded.reason:
                reasons.append(encoded.reason)
            if encoded.digest:
                ext[codec.RESULT_DIGEST_EXT] = encoded.digest
            record["result"] = encoded.result
        if reasons:
            ext[codec.RESULT_CODEC_EXT] = codec.CODEC_NOT_SERVABLE
            ext[codec.NOT_SERVABLE_REASON_EXT] = "; ".join(reasons)
        else:
            ext[codec.RESULT_CODEC_EXT] = codec.CODEC_JSON
        record["extensions"] = ext
        return record
