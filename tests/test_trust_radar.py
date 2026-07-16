"""ADR-0173 (data slice) — Trust Attestation Radar JSON projection.

Pure, read-only projection of a capsule's *verification output* (the 7 Trust-Layer
guarantees) into a fixed-axis radar model. One axis per guarantee, each plotted 0..1
(booleans → 0/1; ``redaction_coverage`` is already a ratio). The polygon's shape *is* the
verdict; a missing guarantee (e.g. an unsealed capsule) is an ``n/a`` axis, distinct from a
failed one.

This is the Python/JSON half of feature F-05 — it feeds the `web/` SVG glyph that ADR-0173
describes; it is NOT the glyph itself. No schema change, no new dependency (ADR-0173 §98).
"""
from __future__ import annotations

from novafabric.trust.radar import (
    AXIS_ORDER,
    AxisState,
    RadarVerdict,
    build_trust_radar,
)

_FULL = {
    "signature_ok": True,
    "timestamp_ok": True,
    "log_integrity_ok": True,
    "redaction_coverage": 1.0,
    "secret_scan_clean": True,
    "policy_pass": True,
    "eval_gate_pass": True,
}


def test_axis_order_is_fixed_and_seven():
    radar = build_trust_radar(_FULL)
    assert [a.key for a in radar.axes] == list(AXIS_ORDER)
    assert len(radar.axes) == 7


def test_full_attestation_is_a_full_polygon():
    radar = build_trust_radar(_FULL)
    assert all(a.value == 1.0 for a in radar.axes)
    assert all(a.state is AxisState.ok for a in radar.axes)
    assert radar.verdict is RadarVerdict.attested


def test_signature_fail_is_critical():
    radar = build_trust_radar({**_FULL, "signature_ok": False})
    sig = next(a for a in radar.axes if a.key == "signature")
    assert sig.value == 0.0
    assert sig.state is AxisState.fail
    assert radar.verdict is RadarVerdict.critical


def test_broken_log_integrity_is_also_critical():
    # a broken Merkle log is tampering — a hard-fail axis, not a mere warning
    radar = build_trust_radar({**_FULL, "log_integrity_ok": False})
    assert radar.verdict is RadarVerdict.critical


def test_unsealed_capsule_has_na_seal_axes_and_unsealed_verdict():
    unsealed = {
        "redaction_coverage": 1.0,
        "secret_scan_clean": True,
        "policy_pass": True,
        "eval_gate_pass": True,
    }  # no signature/timestamp/log_integrity keys at all
    radar = build_trust_radar(unsealed)
    sig = next(a for a in radar.axes if a.key == "signature")
    assert sig.value is None
    assert sig.state is AxisState.na
    # a capsule with no seal cannot be "attested", even if policy/eval are clean
    assert radar.verdict is RadarVerdict.unsealed


def test_missing_axis_is_na_not_fail():
    radar = build_trust_radar({**_FULL, "policy_pass": None})
    policy = next(a for a in radar.axes if a.key == "policy")
    assert policy.state is AxisState.na
    assert policy.value is None


def test_non_hardfail_miss_is_partial_not_critical():
    # timestamp missing-as-fail is a dent, but the seal itself is intact → partial
    radar = build_trust_radar({**_FULL, "timestamp_ok": False})
    ts = next(a for a in radar.axes if a.key == "timestamp")
    assert ts.state is AxisState.fail
    assert radar.verdict is RadarVerdict.partial


def test_redaction_coverage_ratio_maps_to_warn():
    radar = build_trust_radar({**_FULL, "redaction_coverage": 0.5})
    cov = next(a for a in radar.axes if a.key == "redaction_coverage")
    assert cov.value == 0.5
    assert cov.state is AxisState.warn
    assert radar.verdict is RadarVerdict.partial


def test_redaction_coverage_is_clamped():
    hi = next(a for a in build_trust_radar({**_FULL, "redaction_coverage": 1.4}).axes
              if a.key == "redaction_coverage")
    lo = next(a for a in build_trust_radar({**_FULL, "redaction_coverage": -0.2}).axes
              if a.key == "redaction_coverage")
    assert hi.value == 1.0 and hi.state is AxisState.ok
    assert lo.value == 0.0 and lo.state is AxisState.fail


def test_empty_input_is_unsealed():
    radar = build_trust_radar({})
    assert all(a.state is AxisState.na for a in radar.axes)
    assert radar.verdict is RadarVerdict.unsealed


def test_capsule_id_is_carried():
    radar = build_trust_radar(_FULL, capsule_id="run-42")
    assert radar.capsule_id == "run-42"


def test_model_is_json_round_trippable():
    radar = build_trust_radar(_FULL, capsule_id="run-42")
    dumped = radar.model_dump(mode="json")
    assert dumped["verdict"] == "attested"
    assert dumped["axes"][0]["key"] == "signature"
