"""ADR-0159 D2 (first slice) — SR 26-2 / SR 11-7 model-risk evidence pack (NF-271).

An **assembler** over sealed facts: it gathers the four model-risk pillars (development,
independent-validation, ongoing-monitoring, model-inventory) and marks each ``complete`` /
``partial`` / ``missing`` with a source ref or a machine-readable reason. It is *never a
validation and never a model-risk rating* — assemble, don't assess. No field is fabricated: a
``missing`` pillar carries no source refs.
"""
from __future__ import annotations

from novafabric.compliance.export.finance.model_risk import (
    MODEL_RISK_PILLARS,
    SR_REGIME,
    ModelRiskFile,
    PillarEvidence,
    build_model_risk_file,
)


def test_all_pillars_present_are_complete_and_carry_the_regime():
    mrf = build_model_risk_file(
        model_id="agent-7",
        development=["capsule://eval-card-1"],
        independent_validation=["capsule://val-1"],
        ongoing_monitoring=["capsule://drift-1"],
        model_inventory=["capsule://inv-1"],
    )
    assert isinstance(mrf, ModelRiskFile)
    assert mrf.regime == SR_REGIME
    assert all(p.status == "complete" for p in mrf.pillars)
    assert "rating" not in ModelRiskFile.model_fields  # assemble, never assess


def test_pillars_are_the_fixed_four_in_order():
    mrf = build_model_risk_file(model_id="m")
    assert [p.pillar for p in mrf.pillars] == list(MODEL_RISK_PILLARS)


def test_missing_pillar_has_no_refs_and_a_reason():
    mrf = build_model_risk_file(model_id="m", development=["capsule://eval-1"])
    dev = next(p for p in mrf.pillars if p.pillar == "development")
    val = next(p for p in mrf.pillars if p.pillar == "independent_validation")
    assert dev.status == "complete" and dev.source_refs == ["capsule://eval-1"]
    assert val.status == "missing"
    assert val.source_refs == []       # never fabricated
    assert val.reason                   # machine-readable reason present


def test_partial_pillar_keeps_refs_and_is_flagged():
    mrf = build_model_risk_file(
        model_id="m", ongoing_monitoring=["capsule://drift-1"], partial={"ongoing_monitoring"}
    )
    mon = next(p for p in mrf.pillars if p.pillar == "ongoing_monitoring")
    assert mon.status == "partial"
    assert mon.source_refs == ["capsule://drift-1"]
    assert mon.reason


def test_completeness_summary_counts():
    mrf = build_model_risk_file(
        model_id="m",
        development=["d"], independent_validation=["v"],
        # ongoing_monitoring + model_inventory missing
    )
    assert mrf.summary == {"complete": 2, "partial": 0, "missing": 2}


def test_model_id_is_carried():
    assert build_model_risk_file(model_id="agent-42").model_id == "agent-42"


def test_pillar_evidence_carries_no_verdict_field():
    # a pillar records presence, never an assessment/score/pass-fail
    assert "verdict" not in PillarEvidence.model_fields
    assert "score" not in PillarEvidence.model_fields
