"""The first divergent step between two runs (issue #74).

``nova diff`` lists every changed, added and removed call. An auditor's first
question is narrower: *where* did the two runs stop agreeing? This module
answers it over the two event streams the diff aligns — model calls (logical
calls only, ADR-0305) and tool calls — and nothing else: environment and output
differences are not events, so they never produce a first divergence.

The step index is the position of the record in its run's **timeline**:

``started_at``
    Every compared model and tool call in both runs carries a parseable
    ``started_at``: the two streams are merged chronologically per run, so the
    index counts model and tool calls together (step 0 = the run's first call).
``per_stream``
    Some record lacks ``started_at``: no cross-stream order is recorded, so the
    index counts records of that stream only; the stream with the smaller
    position is reported (ties go to model calls), which is no claim about
    which call happened first. The ``timeline`` key says which, so no consumer reads a
    per-stream position as a chronological one.

Indexes count the records that parsed; skipped malformed lines
(``skipped_malformed_lines``) take no position.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from novafabric._oneline import one_line
from novafabric.diff._align import Pair

Record = dict[str, Any]

#: Stream key -> (event type, record-id key). Order is the tie-break order.
STREAMS: dict[str, tuple[str, str]] = {
    "model_calls": ("model_call", "model_call_id"),
    "tool_calls": ("tool_call", "tool_call_id"),
}
TIMELINE_STARTED_AT = "started_at"
#: Sort placeholder; never used for a record in a chronological timeline.
_NO_TIME = datetime.min.replace(tzinfo=UTC)
TIMELINE_PER_STREAM = "per_stream"

#: Report flag -> aspect name, per stream, in the order they are listed.
_ASPECTS: dict[str, tuple[tuple[str, str], ...]] = {
    "model_calls": (
        ("request_changed", "request"),
        ("response_changed", "response"),
        ("provider_changed", "provider"),
        ("params_changed", "params"),
    ),
    "tool_calls": (
        ("arguments_changed", "arguments"),
        ("result_changed", "result"),
        ("status_changed", "status"),
    ),
}


@dataclass(frozen=True)
class StreamComparison:
    """One aligned stream: both sides' records, the pairs, and the report entries.

    ``entries[i]`` is the ``DiffReport`` entry the engine wrote for ``pairs[i]``.
    """

    stream: str
    records_a: Sequence[Record]
    records_b: Sequence[Record]
    pairs: Sequence[Pair]
    entries: Sequence[dict[str, Any]]


def _started_at(record: Record) -> datetime | None:
    value = record.get("started_at")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # A naive stamp is read as UTC so it orders against aware ones.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _positions(
    streams: Sequence[StreamComparison], side: str, chronological: bool
) -> dict[int, int]:
    """``id(record) -> step index`` for one run (``side`` is ``a`` or ``b``)."""
    rows: list[tuple[datetime | None, int, int, Record]] = []
    for order, comparison in enumerate(streams):
        records = comparison.records_a if side == "a" else comparison.records_b
        rows.extend(
            (_started_at(record), order, position, record)
            for position, record in enumerate(records)
        )
    if not chronological:
        return {id(record): position for _, _, position, record in rows}
    rows.sort(key=lambda row: (row[0] or _NO_TIME, row[1], row[2]))
    return {id(row[3]): step for step, row in enumerate(rows)}


def _is_chronological(streams: Sequence[StreamComparison]) -> bool:
    records = [
        record
        for comparison in streams
        for record in (*comparison.records_a, *comparison.records_b)
    ]
    return bool(records) and all(_started_at(record) is not None for record in records)


def _reason(entry: dict[str, Any]) -> str | None:
    for reason in ("added", "removed", "changed"):
        if entry.get(reason):
            return reason
    return None


def first_divergence(streams: Sequence[StreamComparison]) -> dict[str, Any] | None:
    """The earliest changed, added or removed call across ``streams``, or None.

    "Earliest" is the smallest step index on either side (A's index for a
    changed or removed call, B's for an added one); ties go to the stream listed
    first in :data:`STREAMS`.
    """
    chronological = _is_chronological(streams)
    index_a = _positions(streams, "a", chronological)
    index_b = _positions(streams, "b", chronological)
    best: tuple[tuple[int, int], dict[str, Any]] | None = None
    for order, comparison in enumerate(streams):
        event_type, id_key = STREAMS[comparison.stream]
        for (a, b), entry in zip(comparison.pairs, comparison.entries, strict=True):
            reason = _reason(entry)
            if reason is None:
                continue
            ia = index_a[id(a)] if a is not None else None
            ib = index_b[id(b)] if b is not None else None
            step = min(i for i in (ia, ib) if i is not None)
            if best is not None and (step, order) >= best[0]:
                continue
            best = ((step, order), {
                "index": step,
                "index_a": ia,
                "index_b": ib,
                "stream": comparison.stream,
                "event_type": event_type,
                "reason": reason,
                "aspects": [
                    name for flag, name in _ASPECTS[comparison.stream] if entry.get(flag)
                ],
                "record_id_a": a.get(id_key) if a is not None else None,
                "record_id_b": b.get(id_key) if b is not None else None,
                "timeline": TIMELINE_STARTED_AT if chronological else TIMELINE_PER_STREAM,
            })
    return best[1] if best is not None else None


def format_first_divergence(divergence: dict[str, Any] | None) -> list[str]:
    """Human lines: where the runs first diverge, or that their calls agree."""
    if divergence is None:
        return ["First divergence: none in model or tool calls"]
    counted = (
        "model and tool calls, by started_at"
        if divergence.get("timeline") == TIMELINE_STARTED_AT
        else f"{one_line(divergence.get('stream'))} records only"
    )
    sides = []
    for side in ("a", "b"):
        index = divergence.get(f"index_{side}")
        if index is not None:
            record_id = divergence.get(f"record_id_{side}")
            sides.append(
                f"{side.upper()} #{one_line(index)}"
                + (f" {one_line(record_id)}" if record_id is not None else "")
            )
    aspects = divergence.get("aspects") or []
    detail = f" ({one_line(', '.join(str(a) for a in aspects), 120)})" if aspects else ""
    return [
        f"First divergence: step {one_line(divergence.get('index'))} "
        f"{one_line(divergence.get('event_type'))} "
        f"{one_line(divergence.get('reason'))}{detail} [{'; '.join(sides)}]",
        f"  (step counts {counted})",
    ]
