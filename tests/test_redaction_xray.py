"""ADR-0174 (data slice) — Redaction / Secret-scan X-Ray JSON projection.

Projects a capsule's field-structure + redaction/scan metadata into a per-field **state**
overlay (``clear`` / ``redacted`` / ``secret_scrubbed`` / ``never_captured`` / ``unknown``)
with a coverage meter and per-state counts.

The load-bearing invariant (ADR-0174 §1): **values are never shown.** The projection carries
the field *path* and its *state* only — never the captured/redacted/scrubbed value. This is
enforced structurally: :class:`FieldXRay` has no value field at all.
"""
from __future__ import annotations

from novafabric.masking.xray import (
    FieldState,
    FieldXRay,
    build_field_xray,
    field_states_from_findings,
)


def test_fieldxray_has_no_value_field():
    # the invariant, enforced at the type level: only path + state, never a value
    assert set(FieldXRay.model_fields) == {"path", "state"}


def test_counts_cover_all_states_zero_filled():
    report = build_field_xray([{"path": "a", "state": "clear"}])
    assert report.counts == {
        "clear": 1, "redacted": 0, "secret_scrubbed": 0,
        "never_captured": 0, "unknown": 0,
    }


def test_coverage_is_protected_over_sensitive_surface():
    records = [
        {"path": "p1", "state": "redacted"},
        {"path": "p2", "state": "redacted"},
        {"path": "p3", "state": "secret_scrubbed"},
        {"path": "p4", "state": "unknown"},          # unverified → drags coverage down
        {"path": "p5", "state": "clear"},             # not sensitive → excluded
        {"path": "p6", "state": "never_captured"},    # by design → excluded
    ]
    report = build_field_xray(records)
    # sensitive surface = redacted + secret_scrubbed + unknown = 4; protected = 3
    assert report.sensitive_total == 4
    assert report.sensitive_protected == 3
    assert report.coverage == 0.75


def test_coverage_is_none_when_no_sensitive_surface():
    report = build_field_xray([
        {"path": "a", "state": "clear"},
        {"path": "b", "state": "never_captured"},
    ])
    assert report.sensitive_total == 0
    assert report.coverage is None


def test_extra_value_key_in_input_is_never_stored():
    # a caller may hand us a richer record; the model must drop everything but path+state
    report = build_field_xray([{"path": "secret_field", "state": "redacted",
                                "value": "sk-live-DEADBEEF"}])
    field = report.fields[0]
    assert field.path == "secret_field"
    assert field.state is FieldState.redacted
    assert not hasattr(field, "value")
    assert "sk-live-DEADBEEF" not in report.model_dump_json()


def test_field_states_from_findings_maps_strategy_and_drops_values():
    findings = [
        {"target_ref": "prompt.jsonl#L3 $.messages[0].content",
         "redaction_strategy": "mask", "match_hash": "deadbeef", "replacement": "[MASKED:x]"},
        {"target_ref": "env.yaml SECRET_KEY",
         "redaction_strategy": "drop", "match_hash": "cafe", "replacement": ""},
    ]
    fields = field_states_from_findings(findings)
    assert fields[0].path == "prompt.jsonl#L3 $.messages[0].content"
    assert fields[0].state is FieldState.redacted
    assert fields[1].state is FieldState.secret_scrubbed
    # the digest/replacement must not leak into the projection
    dumped = [f.model_dump() for f in fields]
    assert all(set(d) == {"path", "state"} for d in dumped)


def test_json_round_trippable():
    report = build_field_xray([{"path": "a", "state": "redacted"}], capsule_id="run-1")
    dumped = report.model_dump(mode="json")
    assert dumped["capsule_id"] == "run-1"
    assert dumped["coverage"] == 1.0
    assert dumped["fields"][0] == {"path": "a", "state": "redacted"}
