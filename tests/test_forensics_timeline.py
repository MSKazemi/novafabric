"""ADR-0155 D1 (first slice) — forensic timeline as a deterministic view over sealed evidence.

`nova forensics timeline` merges an incident's evidence (runs, sessions, lineage neighbours) into
one **deterministically ordered** event list, tie-broken on ``(ts, source_capsule, seq)`` so
re-running over the same sealed inputs is **byte-identical**. Missing evidence is recorded as
``gaps`` rather than raising (fail-open). This first slice is the pure deterministic merge core;
the incident/session/lineage collector that feeds it is a documented follow-on.
"""
from __future__ import annotations

from novafabric.forensics.timeline import (
    ForensicsTimeline,
    TimelineEvent,
    merge_timeline,
)

_RECORDS = [
    {"ts": "2026-07-16T10:00:02Z", "source_capsule": "run-b", "seq": 0, "kind": "run"},
    {"ts": "2026-07-16T10:00:01Z", "source_capsule": "run-a", "seq": 1, "kind": "session"},
    {"ts": "2026-07-16T10:00:01Z", "source_capsule": "run-a", "seq": 0, "kind": "run"},
    {"ts": "2026-07-16T10:00:01Z", "source_capsule": "run-b", "seq": 0, "kind": "lineage-edge"},
]


def test_events_sorted_by_ts_then_capsule_then_seq():
    tl = merge_timeline("INC-1", _RECORDS)
    order = [(e.ts, e.source_capsule, e.seq) for e in tl.events]
    assert order == sorted(order)  # deterministic total order on the triple
    # first is the earliest ts; the ts-tie is broken by capsule then seq
    assert order[0] == ("2026-07-16T10:00:01Z", "run-a", 0)
    assert order[1] == ("2026-07-16T10:00:01Z", "run-a", 1)
    assert order[2] == ("2026-07-16T10:00:01Z", "run-b", 0)


def test_reordering_input_is_byte_identical():
    tl1 = merge_timeline("INC-1", _RECORDS)
    tl2 = merge_timeline("INC-1", list(reversed(_RECORDS)))
    assert tl1.model_dump_json() == tl2.model_dump_json()


def test_gaps_are_recorded_not_raised_and_deduped_sorted():
    tl = merge_timeline("INC-1", _RECORDS, gaps=["run-z", "run-y", "run-z"])
    assert tl.gaps == ["run-y", "run-z"]  # deduped + deterministically sorted


def test_empty_incident_yields_empty_timeline():
    tl = merge_timeline("INC-1", [])
    assert isinstance(tl, ForensicsTimeline)
    assert tl.events == []
    assert tl.gaps == []


def test_accepts_timeline_event_objects_too():
    ev = TimelineEvent(ts="2026-07-16T09:00:00Z", source_capsule="run-a", seq=0, kind="run")
    tl = merge_timeline("INC-1", [ev])
    assert tl.events[0].source_capsule == "run-a"


def test_incident_id_is_carried():
    assert merge_timeline("INC-42", _RECORDS).incident_id == "INC-42"


def test_detail_is_optional_reference_only():
    # detail is a reference/summary; a record without it is fine (never fabricated)
    tl = merge_timeline("INC-1", [{"ts": "t", "source_capsule": "c", "seq": 0, "kind": "run"}])
    assert tl.events[0].detail is None
