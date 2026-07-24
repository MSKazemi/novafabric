"""Forensic timeline — a deterministic view over an incident's sealed evidence (ADR-0155 D1).

`merge_timeline` folds an incident's evidence records (runs, sessions, lineage neighbours) into one
**deterministically ordered** :class:`ForensicsTimeline`, tie-broken on ``(ts, source_capsule,
seq)`` so that re-running over the same sealed inputs is **byte-identical**. Evidence that could not
be reconstructed is recorded in ``gaps`` (deduped + sorted) rather than raising — the timeline is
fail-open, per the ADR ("records missing evidence as gaps rather than raising").

This is the pure deterministic merge core. It reconstructs only over already-sealed evidence a
caller supplies; the incident → session → lineage *collector* that gathers those records (composing
the shipped incident store, session view, and lineage read API) is a documented follow-on slice.

Determinism note: ``ts`` is compared as an opaque string. Callers supply the sealed ISO-8601
timestamp verbatim; lexicographic order on a zero-padded UTC ISO-8601 instant is chronological.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel


class TimelineEvent(BaseModel):
    ts: str  # sealed ISO-8601 timestamp (opaque string; supplied verbatim, never the system clock)
    source_capsule: str  # the sealed capsule this event was reconstructed from
    seq: int  # per-capsule sequence, the final tie-breaker
    kind: str  # e.g. "run" | "session" | "lineage-edge"
    detail: str | None = None  # a reference/summary — never a raw value or PII


#: The compliance-honesty line the NF-231-240 spec makes normative: "every
#: exported artifact MUST carry the compliance-honesty line". Carried as a
#: model FIELD rather than printed only by the CLI, so it survives
#: `--json` and any downstream consumer — the same shape `export_part11.py`
#: already uses. A banner the CLI prints but the payload omits is absent
#: exactly where the artifact travels furthest from the person who ran it.
HONESTY_LINE: str = (
    "NovaFabric reconstructs this timeline from sealed evidence and records gaps it "
    "could not reconstruct. It does not establish causation, attribute fault, or "
    "certify the timeline complete."
)


class ForensicsTimeline(BaseModel):
    incident_id: str
    events: list[TimelineEvent]
    gaps: list[str]  # references to evidence that could not be reconstructed (deduped, sorted)
    honesty_line: str = HONESTY_LINE


def _sort_key(event: TimelineEvent) -> tuple[str, str, int]:
    return (event.ts, event.source_capsule, event.seq)


def merge_timeline(
    incident_id: str,
    records: Iterable[Mapping[str, Any] | TimelineEvent],
    *,
    gaps: Iterable[str] = (),
) -> ForensicsTimeline:
    """Merge evidence ``records`` into a deterministically ordered :class:`ForensicsTimeline`.

    Records may be mappings or :class:`TimelineEvent` objects. The result is ordered by
    ``(ts, source_capsule, seq)`` — a total order that makes re-runs byte-identical — and ``gaps``
    are deduplicated and sorted for the same reason.
    """
    events = [
        rec if isinstance(rec, TimelineEvent) else TimelineEvent.model_validate(rec)
        for rec in records
    ]
    events.sort(key=_sort_key)
    return ForensicsTimeline(
        incident_id=incident_id,
        events=events,
        gaps=sorted(set(gaps)),
    )
