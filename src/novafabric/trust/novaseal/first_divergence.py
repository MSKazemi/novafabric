"""Where a sealed capsule first stops matching its seal (issue #74).

``nova verify`` checks the ADR-0251 ``evidence_digests``: **one sha256 per
file**, made at seal time. That is the finest granularity the seal supports.
An edited ``model-calls.jsonl`` is detected, but no per-record digest exists, so
*which* record was edited cannot be established from the seal — and this module
never guesses: ``index`` is always ``None`` and ``granularity`` is ``"file"``.

What it adds is a name for the divergence an auditor reads first: the first
evidence file that is modified or missing, preferring the capsule's event
streams (model calls, tool calls, trace, …) over other files, each group in
capsule-relative path order. The seal records no time order between files, so
"first" is this fixed precedence, not a claim about when the edit happened.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from novafabric._oneline import PATH_LIMIT, one_line

if TYPE_CHECKING:
    from novafabric.trust.novaseal.capsule_verify import CapsuleBinding

#: Default event-stream file -> (stream key, event type). The keys match
#: ``nova diff``'s ``first_divergence.stream`` where the two overlap.
EVENT_STREAM_FILES: dict[str, tuple[str, str]] = {
    "model-calls.jsonl": ("model_calls", "model_call"),
    "tool-calls.jsonl": ("tool_calls", "tool_call"),
    "trace.jsonl": ("trace", "span"),
    "network_events.jsonl": ("network_events", "network_event"),
    "file_events.jsonl": ("file_events", "file_event"),
    "human_approvals.jsonl": ("human_approvals", "human_approval"),
    "assets.jsonl": ("assets", "asset"),
}
GRANULARITY_FILE = "file"


def binding_first_divergence(binding: CapsuleBinding) -> dict[str, Any] | None:
    """The first modified or missing evidence file, or None when every digest held.

    None also when the capsule was sealed before ``evidence_digests`` existed:
    nothing per-file was checked, so nothing can be named.
    """
    if not binding.digests_present:
        return None
    reasons = dict.fromkeys(binding.mismatched, "modified")
    reasons.update(dict.fromkeys(binding.missing, "missing"))
    if not reasons:
        return None
    first = min(reasons, key=lambda rel: (rel not in EVENT_STREAM_FILES, rel))
    stream, event_type = EVENT_STREAM_FILES.get(first, (None, None))
    return {
        "path": first,
        "stream": stream,
        "event_type": event_type,
        "index": None,
        "reason": reasons[first],
        "granularity": GRANULARITY_FILE,
        "divergent_files": len(reasons),
    }


def format_binding_first_divergence(divergence: dict[str, Any]) -> list[str]:
    """Human lines naming the first divergent file and the granularity limit."""
    kind = divergence.get("event_type")
    what = f"{one_line(kind)} records" if kind else "evidence file"
    more = int(divergence.get("divergent_files") or 1) - 1
    also = f" ({more} more file(s) also diverge)" if more > 0 else ""
    return [
        f"First divergence: {one_line(divergence.get('path'), PATH_LIMIT)} ({what}) "
        f"{one_line(divergence.get('reason'))} after sealing{also}",
        "  record index not locatable: the seal holds one sha256 per file",
    ]
