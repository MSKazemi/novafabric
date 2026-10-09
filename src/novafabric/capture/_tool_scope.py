"""The enclosing ``record.tool`` boundary, for nested-record marking (ADR-0306 D3).

A decorated function may itself call a model or an MCP tool. While its body runs,
the boundary's pre-allocated ``tool_call_id`` sits in a context variable;
``CapsuleWriter.append_model_call`` / ``append_tool_call`` -- the one write path
every in-process hook passes through -- call :func:`mark_nested`, which stamps
``extensions["io.novafabric.within_tool_call_id"]`` on the record and counts it.
A boundary with nested records is recorded as not servable in slice 1.

A raw thread inherits no context (ADR-0224 D3), so a nested call made from one
goes unmarked; its record is then left unconsumed and a strict replay fails
closed. Never raises into the workload.
"""

from __future__ import annotations

import threading
from contextvars import ContextVar, Token
from typing import Any

WITHIN_TOOL_CALL_EXT = "io.novafabric.within_tool_call_id"


class _Boundary:
    __slots__ = ("tool_call_id", "nested", "_lock")

    def __init__(self, tool_call_id: str) -> None:
        self.tool_call_id = tool_call_id
        self.nested = 0
        self._lock = threading.Lock()

    def count(self) -> None:
        with self._lock:
            self.nested += 1


_boundary: ContextVar[_Boundary | None] = ContextVar(
    "novafabric_record_tool_boundary", default=None
)


def enter(tool_call_id: str) -> tuple[_Boundary, Token[_Boundary | None]]:
    boundary = _Boundary(tool_call_id)
    return boundary, _boundary.set(boundary)


def leave(token: Token[_Boundary | None]) -> None:
    try:
        _boundary.reset(token)
    except ValueError:  # reset from another context: restore by value instead
        _boundary.set(token.old_value if token.old_value is not Token.MISSING else None)


def mark_nested(record: Any) -> None:
    """Stamp *record* with the enclosing boundary, if any, and count it."""
    try:
        boundary = _boundary.get()
        if boundary is None or not isinstance(record, dict):
            return
        ext = record.get("extensions")
        if ext is None:
            ext = record["extensions"] = {}
        if not isinstance(ext, dict):
            return
        ext[WITHIN_TOOL_CALL_EXT] = boundary.tool_call_id
        boundary.count()
    except Exception:  # noqa: BLE001 -- capture must never block the workload
        pass
