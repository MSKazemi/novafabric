"""One replayed tool call is one entry, whatever events it wrote.

A call an intercepted surface cannot serve writes a tool divergence
(``tool_call_unmatched`` / ``tool_result_not_servable``) first; under
``--permissive`` the same call then writes ``tool_live`` (it ran live) or
``tool_refused`` (it was refused), marked :data:`AFTER_DIVERGENCE`. The event log
keeps both -- the contract counters count the divergence *and* the outcome -- but
the replayed tool-call sequence (``_fidelity``) and the replay capsule's
``tool-calls.jsonl`` (``_replay_capsule``) list calls, so the pair collapses into
one entry: the call's final outcome, at the divergence's place in the log.

Pairing is by ``(pid, surface, tool_name)``, earliest divergence first, so calls
of one tool made in parallel (LangGraph ``ToolNode`` threads) each pair with one
divergence. Fail closed: an outcome event with no divergence to pair with stays
its own entry; nothing is dropped.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Final

from novafabric.replay._contract import _TOOL_DIVERGENCE_KINDS, TOOL_SURFACE_MCP

#: Set (``True``) on the ``tool_live`` / ``tool_refused`` event a server writes
#: right after reporting that call's divergence.
AFTER_DIVERGENCE: Final[str] = "after_divergence"

_OUTCOMES: Final = frozenset({"tool_live", "tool_refused"})


def _key(ev: dict[str, Any]) -> tuple[Any, str, str]:
    return (ev.get("pid"), str(ev.get("surface") or TOOL_SURFACE_MCP), str(ev.get("tool_name")))


def collapse_tool_divergences(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """*events* with each tool divergence that a marked outcome event followed
    replaced by that outcome event (which is dropped from its own place)."""
    out: list[dict[str, Any]] = []
    pending: dict[tuple[Any, str, str], deque[int]] = {}
    for ev in events:
        kind = ev.get("event")
        if kind == "divergence" and ev.get("kind") in _TOOL_DIVERGENCE_KINDS:
            pending.setdefault(_key(ev), deque()).append(len(out))
        elif kind in _OUTCOMES and ev.get(AFTER_DIVERGENCE) is True:
            queue = pending.get(_key(ev))
            if queue:
                out[queue.popleft()] = ev
                continue
        out.append(ev)
    return out
