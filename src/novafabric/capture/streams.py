"""The capsule's JSONL stream names, in one dependency-free place.

Imported by the workload-side ``sitecustomize`` path (via
:mod:`novafabric.capture.workload_health`), so it must stay cheap to import:
no regexes, no scanner. ``tests/capture/test_capture_hostile_workload.py``
asserts it covers every structured stream the secret scanner scans.
"""

from __future__ import annotations

from typing import Final

__all__ = ["CAPSULE_STREAMS"]

#: Every JSONL stream a capture hook, the capsule writer or the event recorder
#: appends to. The only stream names a workload's capture-health hand-off may
#: report drops for.
CAPSULE_STREAMS: Final[frozenset[str]] = frozenset(
    {
        "model-calls.jsonl",
        "tool-calls.jsonl",
        "trace.jsonl",
        "assets.jsonl",
        "tool-permission-events.jsonl",
        "file_events.jsonl",
        "network_events.jsonl",
        "human_approvals.jsonl",
        "state_transitions.jsonl",
        "memory_operations.jsonl",
        "guardrail_events.jsonl",
        "evaluator_events.jsonl",
        "reranker_events.jsonl",
        "vector_retrievals.jsonl",
    }
)
