"""Call alignment for ``nova diff`` (the private design/spec/diff-report-v1.md).

Model calls: exact ``parent_span_id`` match first, then sequence position.
Tool calls: exact ``(tool_name, arg_hash)`` match, then sequence position.

The span pass only pairs span ids that are unique on both sides: every call of one
``nova capture`` shares the capture's root span, and every capture gets a fresh one,
so span ids alone can neither pair calls within a capture nor across two captures.

The sequence pass anchors on calls whose request is identical (a longest common
subsequence, via :class:`difflib.SequenceMatcher`), so an inserted or deleted call
does not shift every later pairing. Calls between two anchors pair by position --
a changed prompt or model is a *changed* pair, not an add plus a remove. Tool calls
between anchors pair by position only when the tool name matches.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Hashable
from difflib import SequenceMatcher
from typing import Any

Call = dict[str, Any]
Pair = tuple[Call | None, Call | None]


def _arg_hash(arguments: Any) -> str:
    if not isinstance(arguments, dict):
        arguments = {}
    return hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()[:16]


def _request_key(call: Call) -> str:
    request = {
        "system": call.get("gen_ai.system"),
        "model": call.get("gen_ai.request.model"),
        "messages": call.get("gen_ai.request.messages"),
    }
    return hashlib.sha256(
        json.dumps(request, sort_keys=True, default=str).encode()
    ).hexdigest()


def _tool_key(call: Call) -> tuple[str, str]:
    return (str(call.get("tool_name", "")), _arg_hash(call.get("arguments", {})))


def _sequence_align(
    calls_a: list[Call],
    calls_b: list[Call],
    anchor_key: Callable[[Call], Hashable],
    gap: Callable[[list[Call], list[Call]], list[Pair]],
) -> list[Pair]:
    """Anchor on equal keys (LCS), then resolve each gap between anchors with ``gap``."""
    keys_a = [anchor_key(c) for c in calls_a]
    keys_b = [anchor_key(c) for c in calls_b]
    pairs: list[Pair] = []
    matcher = SequenceMatcher(None, keys_a, keys_b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            pairs.extend(zip(calls_a[i1:i2], calls_b[j1:j2], strict=True))
        else:
            pairs.extend(gap(calls_a[i1:i2], calls_b[j1:j2]))
    return pairs


def _positional_gap(gap_a: list[Call], gap_b: list[Call]) -> list[Pair]:
    pairs: list[Pair] = list(zip(gap_a, gap_b, strict=False))
    pairs.extend((a, None) for a in gap_a[len(gap_b):])
    pairs.extend((None, b) for b in gap_b[len(gap_a):])
    return pairs


def _same_tool_gap(gap_a: list[Call], gap_b: list[Call]) -> list[Pair]:
    # Within a gap, pair by position only where the tool name agrees.
    return _sequence_align(
        gap_a,
        gap_b,
        lambda c: str(c.get("tool_name", "")),
        lambda ga, gb: [(a, None) for a in ga] + [(None, b) for b in gb],
    )


def align_model_calls(
    calls_a: list[Call],
    calls_b: list[Call],
) -> list[Pair]:
    """Align model calls by span_id first, then by sequence position."""
    spans_a = Counter(c.get("parent_span_id") or "" for c in calls_a)
    spans_b = Counter(c.get("parent_span_id") or "" for c in calls_b)
    unique = {s for s in spans_a if s and spans_a[s] == 1 and spans_b.get(s) == 1}

    b_by_span = {c.get("parent_span_id"): c for c in calls_b if c.get("parent_span_id") in unique}
    span_pairs: list[Pair] = [
        (a, b_by_span[a.get("parent_span_id")])
        for a in calls_a
        if a.get("parent_span_id") in unique
    ]
    rest_a = [c for c in calls_a if c.get("parent_span_id") not in unique]
    rest_b = [c for c in calls_b if c.get("parent_span_id") not in unique]
    return span_pairs + _sequence_align(rest_a, rest_b, _request_key, _positional_gap)


def align_tool_calls(
    calls_a: list[Call],
    calls_b: list[Call],
) -> list[Pair]:
    """Align tool calls by (tool_name, arg_hash), then sequence position.

    The exact pass ignores order -- parallel tool calls may finish in a different
    order on every run -- and uses each B call at most once. Only the calls left
    over pair by position, and only with a call of the same tool name.
    """
    unused_b: dict[tuple[str, str], list[int]] = {}
    for j, b in enumerate(calls_b):
        unused_b.setdefault(_tool_key(b), []).append(j)

    exact: dict[int, int] = {}
    for i, a in enumerate(calls_a):
        slots = unused_b.get(_tool_key(a))
        if slots:
            exact[i] = slots.pop(0)

    used_b = set(exact.values())
    rest_a = [a for i, a in enumerate(calls_a) if i not in exact]
    rest_b = [b for j, b in enumerate(calls_b) if j not in used_b]
    rest_pairs = _same_tool_gap(rest_a, rest_b)
    rest_by_a = {id(a): b for a, b in rest_pairs if a is not None}

    pairs: list[Pair] = []
    for i, a in enumerate(calls_a):
        pairs.append((a, calls_b[exact[i]] if i in exact else rest_by_a.get(id(a))))
    pairs.extend((None, b) for a, b in rest_pairs if a is None)
    return pairs
