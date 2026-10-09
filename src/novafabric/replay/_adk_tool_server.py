"""Serves Google ADK tool calls in a mocked replay (ADR-0306 slice 3, experimental).

Registered by ``MockToolDispatcher.install`` as the handler of
``adapters._adk_tool_seam``, so the NovaFabric ADK tool plugin asks it *before*
ADK runs the tool. The ADR-0300 contract applies unchanged:

* one-to-one matching, by the model's ``function_call_id`` when the replayed
  call carries the recorded one with the same name **and** the same arguments,
  else by name + after-redaction argument digest, earliest record first;
* an unmatched or unservable call is refused before the tool runs (fail closed);
  ``--permissive`` runs it live only when the operator's ladder flag permits the
  mutation class declared in the plugin (the workload's code, never the capsule);
* ``replay.yaml`` ``tool_overrides`` are applied through
  ``_policy.decide_intercepted`` -- the same decision as ``record.tool`` and MCP.

ADK wraps any exception a plugin callback raises in ``RuntimeError``
(``google/adk/plugins/plugin_manager.py:316-322``); the divergence is written to
the event log *before* the raise, so the replay is a failure even when the
workload swallows it.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from novafabric.adapters._adk_tool_seam import FUNCTION_CALL_ID_EXT, AdkToolCall
from novafabric.capture import _tool_codec
from novafabric.replay._contract import (
    TOOL_SURFACE_ADK,
    ReplayEventLog,
    ToolCallMatcher,
    ToolMatch,
    not_servable_reason,
    record_arguments_digest,
)
from novafabric.replay._errors import (
    ReplayDivergenceError,
    ReplayToolRecordNotServableError,
    ReplayToolUnmatchedError,
)
from novafabric.replay._policy import decide_intercepted

#: Served in place of a recorded ``None``: ADK would have turned ``None`` into
#: exactly this function response (``_caller.py`` ``_normalize_tool_result``),
#: and a plugin answer of ``None`` means "run the tool".
_NONE_RESPONSE: dict[str, Any] = {"result": None}


def _function_call_id(record: dict[str, Any]) -> str | None:
    ext = record.get("extensions")
    value = ext.get(FUNCTION_CALL_ID_EXT) if isinstance(ext, dict) else None
    return value if isinstance(value, str) and value else None


class AdkToolServer:
    """The ADK-surface replay server: one matcher, ADK records only."""

    #: Read by ``AdkToolHook.install``: a capture sink never displaces it.
    serves_replay = True

    def __init__(
        self,
        records: list[dict[str, Any]],
        *,
        divergence_policy: str,
        events: ReplayEventLog,
        permitted: frozenset[str],
        overrides: dict[str, bool] | None = None,
    ) -> None:
        counts = Counter(_function_call_id(r) for r in records)
        #: unique recorded function_call_id -> (tool name, argument digest).
        self._by_call_id: dict[str, tuple[Any, str]] = {}
        #: id(matcher view) -> the record as stored in the capsule.
        self._originals: dict[int, dict[str, Any]] = {}
        views: list[dict[str, Any]] = []
        for record in records:
            view = dict(record)
            call_id = _function_call_id(record)
            if call_id is not None and counts[call_id] == 1:
                # The matcher's id tier compares ``tool_call_id``; an ambiguous
                # (repeated) function_call_id is left out of it.
                view["tool_call_id"] = call_id
                self._by_call_id[call_id] = (
                    record.get("tool_name"), record_arguments_digest(record)
                )
            views.append(view)
            self._originals[id(view)] = record
        self._matcher = ToolCallMatcher(views)
        self._policy = divergence_policy
        self._events = events
        self._permitted = permitted
        self._overrides = dict(overrides or {})

    # ── handler protocol (adapters._adk_tool_seam.AdkToolHandler) ────────────

    def before(self, call: AdkToolCall) -> Any:
        """The value to answer *call* with, ``None`` to run the tool live; raises
        to refuse."""
        override = self._overrides.get(call.tool_name)
        if override is True and call.mutation_class in self._permitted:
            # ADR-0306 D8: `allow: true` plus the operator's ladder flag --
            # re-execute; a matching record is consumed, never served.
            consumed = call.not_canonical is None and self._match(call).record is not None
            self._events.emit(
                "tool_live", tool_name=call.tool_name, surface=TOOL_SURFACE_ADK,
                mutation_class=call.mutation_class, override="allow", consumed=consumed,
            )
            return None
        if call.not_canonical is not None:
            return self._diverge(
                call, ReplayToolRecordNotServableError, None,
                "cannot be matched", call.not_canonical, consumed=False,
            )
        match = self._match(call)
        if match.record is None:
            return self._diverge(
                call, ReplayToolUnmatchedError, call.digest,
                "has no unconsumed recorded result", str(match.reason),
            )
        record = self._originals.get(id(match.record), match.record)
        reason = not_servable_reason(record)
        if reason is None and record.get("status", "success") != "success":
            reason = "the record is a failure, and ADK tool failures are not served"
        if reason is not None:
            return self._diverge(
                call, ReplayToolRecordNotServableError, call.digest,
                "matched a recorded call that cannot be served", reason, consumed=True,
            )
        self._events.emit(
            "tool_mocked",
            tool_name=call.tool_name,
            record_id=record.get("tool_call_id"),
            how=match.how,
            surface=TOOL_SURFACE_ADK,
        )
        _ok, value = _tool_codec.decode_result(record)
        return _NONE_RESPONSE.copy() if value is None else value

    def after(self, tool_context: Any, result: Any) -> None:
        return None

    def on_error(self, tool_context: Any, error: BaseException) -> None:
        return None

    # ── internals ────────────────────────────────────────────────────────────

    def _match(self, call: AdkToolCall) -> ToolMatch:
        digest = str(call.digest)
        call_id = call.function_call_id
        # The id tier only when the recorded call with that id has the same name
        # and the same arguments: an id never serves an answer to other arguments.
        use_id = (
            call_id
            if call_id is not None and self._by_call_id.get(call_id) == (call.tool_name, digest)
            else None
        )
        return self._matcher.match_digest(use_id, call.tool_name, digest)

    def _diverge(
        self, call: AdkToolCall, error_cls: type[ReplayDivergenceError], digest: str | None,
        what: str, reason: str, **extra: Any,
    ) -> Any:
        from novafabric.replay._dispatcher import (
            _divergence_outcome,
            _override_label,
            _report_divergence,
        )

        override = self._overrides.get(call.tool_name)
        permitted = call.mutation_class in self._permitted
        live = decide_intercepted(
            override=override, servable_match=False, permitted=permitted,
            permissive=self._policy == "warn",
        ) == "live"
        outcome = _divergence_outcome(
            live=live, policy=self._policy, override=override,
            gating=call.mutation_class, permitted=permitted, subject="the ADK tool",
        )
        label = _override_label(override)
        if label is not None:
            extra["override"] = label
        error = error_cls(
            f"{TOOL_SURFACE_ADK}({call.tool_name!r}) {what}: {reason}; {outcome}",
            tool_name=call.tool_name,
            arguments_hash=digest,
            reason=reason,
            surface=TOOL_SURFACE_ADK,
            mutation_class=call.mutation_class,
            **extra,
        )
        _report_divergence(self._events, self._policy, error)  # raises under ``fail``
        if live:
            self._events.emit(
                "tool_live", tool_name=call.tool_name, surface=TOOL_SURFACE_ADK,
                mutation_class=call.mutation_class,
            )
            return None
        self._events.emit(
            "tool_refused", tool_name=call.tool_name, surface=TOOL_SURFACE_ADK,
            mutation_class=call.mutation_class,
            **({"override": label} if label is not None else {}),
        )
        raise error
