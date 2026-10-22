"""Evidence this NovaFabric version reads but does not capture (L18).

Compliance exporters must not report a control as *failed* because a capsule lacks
evidence that no NovaFabric producer writes — the operator cannot fix that. They
report it as **not captured by this version** instead, and evaluate the evidence
normally whenever a capsule does carry it (an SDK or third-party writer can call
``CapsuleWriter.append_tool_permission_event``).

Today that is one stream: ``tool-permission-events.jsonl``. ``nova capture`` has no
source of tool-permission decisions, and ``policy._engine.record_tool_permission_event``
writes to a SQLite index, never to the capsule. Retire an entry here the day a
producer lands (``tests/compliance/test_tool_permission_not_captured.py`` fails then).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from novafabric._capsule_fs import is_capsule_regular_file

#: The capsule stream the exporters read for tool-permission decisions.
TOOL_PERMISSION_EVENTS_FILE = "tool-permission-events.jsonl"

#: Older capsules used an underscore spelling; exporters accept both.
TOOL_PERMISSION_EVENTS_LEGACY_FILE = "tool_permission_events.jsonl"

NOT_CAPTURED_REASON = (
    "not captured by this version: NovaFabric does not write "
    f"{TOOL_PERMISSION_EVENTS_FILE}, so its absence is not evidence of a failed control"
)

#: Audit-engine evidence type -> the capsule files that would carry it.
NOT_CAPTURED_EVIDENCE_TYPES: dict[str, tuple[str, ...]] = {
    "tool_permissions": (TOOL_PERMISSION_EVENTS_FILE, TOOL_PERMISSION_EVENTS_LEGACY_FILE),
    "policy_events": (TOOL_PERMISSION_EVENTS_FILE, TOOL_PERMISSION_EVENTS_LEGACY_FILE),
}


def evidence_is_uncaptured(evidence_type: str, capsule_dirs: Iterable[Path]) -> bool:
    """True when *evidence_type* is one this version never captures and no capsule
    in *capsule_dirs* carries its source file (so absence says nothing)."""
    sources = NOT_CAPTURED_EVIDENCE_TYPES.get(evidence_type)
    if sources is None:
        return False
    return not any(is_capsule_regular_file(cd, name) for cd in capsule_dirs for name in sources)


def not_captured_note(description: str) -> str:
    """Human-readable line for a requirement that is not captured by this version."""
    return f"{description} — {NOT_CAPTURED_REASON}"


__all__ = [
    "NOT_CAPTURED_EVIDENCE_TYPES",
    "NOT_CAPTURED_REASON",
    "TOOL_PERMISSION_EVENTS_FILE",
    "TOOL_PERMISSION_EVENTS_LEGACY_FILE",
    "evidence_is_uncaptured",
    "not_captured_note",
]
