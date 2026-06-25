from __future__ import annotations

import hashlib
import json
from typing import Any


def _arg_hash(arguments: Any) -> str:
    if not isinstance(arguments, dict):
        arguments = {}
    return hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()[:16]


def align_model_calls(
    calls_a: list[dict[str, Any]],
    calls_b: list[dict[str, Any]],
) -> list[tuple[dict[str, Any] | None, dict[str, Any] | None]]:
    """Align model calls by span_id first, then by sequence position."""
    by_span_b: dict[str, dict[str, Any]] = {}
    unmatched_b: list[dict[str, Any]] = []
    for c in calls_b:
        span = c.get("parent_span_id", "")
        if span and span not in by_span_b:
            by_span_b[span] = c
        else:
            unmatched_b.append(c)

    pairs: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] = []
    matched_b_ids: set[str] = set()

    for a in calls_a:
        span = a.get("parent_span_id", "")
        b_match = by_span_b.get(span)
        if b_match is not None:
            matched_b_ids.add(b_match.get("model_call_id", ""))
            pairs.append((a, b_match))
        else:
            pairs.append((a, None))

    # Unmatched b calls (added)
    for b in calls_b:
        if b.get("model_call_id", "") not in matched_b_ids:
            pairs.append((None, b))

    return pairs


def align_tool_calls(
    calls_a: list[dict[str, Any]],
    calls_b: list[dict[str, Any]],
) -> list[tuple[dict[str, Any] | None, dict[str, Any] | None]]:
    """Align tool calls by (tool_name, arg_hash), then sequence position."""
    by_sig_b: dict[tuple[str, str], dict[str, Any]] = {}
    for c in calls_b:
        name = c.get("tool_name", "")
        sig = (name, _arg_hash(c.get("arguments", {})))
        if sig not in by_sig_b:
            by_sig_b[sig] = c

    pairs: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] = []
    matched_b_ids: set[str] = set()

    for a in calls_a:
        name = a.get("tool_name", "")
        sig = (name, _arg_hash(a.get("arguments", {})))
        b_match = by_sig_b.get(sig)
        if b_match is not None:
            matched_b_ids.add(b_match.get("tool_call_id", ""))
            pairs.append((a, b_match))
        else:
            pairs.append((a, None))

    for b in calls_b:
        if b.get("tool_call_id", "") not in matched_b_ids:
            pairs.append((None, b))

    return pairs
