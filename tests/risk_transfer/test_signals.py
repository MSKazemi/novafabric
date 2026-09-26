# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""ADR-0170 P2 — NF-384 SLA breach + NF-386 coverage trigger.

Both compare observed facts with *declared* terms and record the comparison;
neither decides a remedy, coverage, a claim or a payout.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError

from novafabric.risk_transfer import (
    CoverageTrigger,
    DeterminationFieldRejectedError,
    InconsistentComparisonError,
    InvalidDecimalError,
    InvalidReferenceError,
    SlaBreach,
    UnboundedFieldError,
    attach_facet,
    build_coverage_trigger,
    build_facet,
    build_sla_breach,
    compare,
    digest_artifact,
)
from novafabric.risk_transfer.signals import (
    MAX_CONDITIONS,
    MAX_EXCLUSIONS,
    MAX_TRIGGER_FACTS,
    MatchedExclusion,
    ParametricCondition,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "risk-transfer"
SCHEMA_PATH = REPO_ROOT / "schemas" / "run-capsule.schema.json"
BASELINE = REPO_ROOT / "tests" / "fixtures" / "model-provenance" / "valid-text-only-capsule.json"
SLA_REF = digest_artifact("sla-terms")
EXCL_REF = digest_artifact("exclusion-set")


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(), parse_float=Decimal)


def _sla(**kw: Any) -> SlaBreach:
    base: dict[str, Any] = {
        "sla_ref": SLA_REF,
        "metric": "availability",
        "operator": "gte",
        "threshold": "99.9",
        "observed_value": "99.2",
        "window": "2026-07",
    }
    base.update(kw)
    return build_sla_breach(**base)


def _coverage(**kw: Any) -> CoverageTrigger:
    declared = _load("coverage-exclusions-cg4047.json")
    facts = _load("coverage-facts-valid.json")
    base: dict[str, Any] = {
        "covered_event_kind": "erroneous_output",
        "exclusion_set_ref": EXCL_REF,
        "trigger_facts": facts["trigger_facts"],
        "declared_exclusions": declared["exclusions"],
        "parametric_terms": declared["parametric_triggers"],
        "observations": facts["observations"],
    }
    base.update(kw)
    return build_coverage_trigger(**base)


# ── compare ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("observed", "op", "threshold", "expected"),
    [
        ("1", "lt", "2", True),
        ("2", "lt", "2", False),
        ("2", "lte", "2", True),
        ("3", "gt", "2", True),
        ("2", "gte", "2.0", True),
        ("2.00", "eq", "2", True),
        ("0.3", "eq", Decimal("0.1") * 3, True),
    ],
)
def test_compare_is_exact(observed: str, op: str, threshold: Any, expected: bool) -> None:
    assert compare(observed, op, threshold) is expected


def test_compare_refuses_float_and_unknown_operator() -> None:
    with pytest.raises(InvalidDecimalError):
        compare(0.3, "eq", "0.3")
    with pytest.raises(ValueError, match="operator"):
        compare("1", "approx", "1")


# ── NF-384 SLA breach ─────────────────────────────────────────────────────


def test_golden_sla_terms_record_a_breach_and_validate_against_schema() -> None:
    terms = _load("sla-terms-valid.json")
    record = build_sla_breach(
        sla_ref=digest_artifact((FIXTURES / "sla-terms-valid.json").read_bytes()),
        observed_value="99.2",
        evidence_refs=[digest_artifact("uptime-probe")],
        **terms,
    )
    assert record.breach is True
    capsule = json.loads(BASELINE.read_text())
    out = attach_facet(capsule, build_facet(sla_breach=record))
    jsonschema.validate(out, json.loads(SCHEMA_PATH.read_text()))
    body = out["facets"]["risk_transfer"]["sla_breach"]
    # Exact decimals serialised as strings, never floats.
    assert body["threshold"] == "99.9" and body["observed_value"] == "99.2"
    assert SlaBreach.model_validate(body).breach is True


def test_golden_nan_threshold_is_rejected() -> None:
    terms = _load("sla-terms-invalid-nan.json")
    with pytest.raises(InvalidDecimalError):
        build_sla_breach(sla_ref=SLA_REF, observed_value="99.2", **terms)


def test_golden_forged_breach_flag_is_rejected() -> None:
    with pytest.raises(InconsistentComparisonError):
        SlaBreach.model_validate(_load("sla-breach-invalid-forged.json"))


def test_commitment_held_is_no_breach() -> None:
    assert _sla(observed_value="99.95").breach is False


def test_boundary_value_meets_gte_commitment() -> None:
    assert _sla(observed_value="99.9").breach is False


def test_ceiling_commitment() -> None:
    rec = _sla(metric="latency_p99_ms", operator="lte", threshold=500, observed_value="501")
    assert rec.breach is True


def test_float_observed_value_is_refused() -> None:
    with pytest.raises(InvalidDecimalError):
        _sla(observed_value=99.2)


def test_sla_has_no_remedy_field_and_rejects_one() -> None:
    assert not {"remedy", "damages", "service_credit"} & set(SlaBreach.model_fields)
    body = _sla().model_dump(mode="json")
    body["service_credit_due"] = "10"
    with pytest.raises(DeterminationFieldRejectedError):
        SlaBreach.model_validate(body)


def test_sla_ref_must_be_digest() -> None:
    with pytest.raises(InvalidReferenceError):
        _sla(sla_ref="SLA-2026-ACME")


@pytest.mark.parametrize(
    ("field", "value"),
    [("metric", "Avail ability"), ("window", ""), ("unit", "x" * 40), ("metric", "a" * 80)],
)
def test_sla_labels_are_bounded(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        _sla(**{field: value})


def test_sla_evidence_refs_bounded_and_digest_only() -> None:
    with pytest.raises(InvalidReferenceError):
        _sla(evidence_refs=["probe-log.txt"])
    with pytest.raises(UnboundedFieldError):
        _sla(evidence_refs=[SLA_REF] * 65)


def test_sla_dump_is_deterministic() -> None:
    a = json.dumps(_sla().model_dump(mode="json"), sort_keys=True)
    b = json.dumps(_sla().model_dump(mode="json"), sort_keys=True)
    assert a == b


# ── NF-386 coverage trigger ───────────────────────────────────────────────


def test_golden_coverage_trigger_records_matched_exclusion_facts() -> None:
    record = _coverage()
    facts = _load("coverage-facts-valid.json")["trigger_facts"]
    assert record.trigger_facts == [f["fact_ref"] for f in facts]
    assert [m.exclusion_id for m in record.matched_exclusions] == ["CG 40 47 (01 26)"]
    assert record.matched_exclusions[0].fact_refs == [facts[1]["fact_ref"]]
    conds = record.parametric_conditions
    assert conds is not None
    assert conds[0].condition_met is True and conds[0].observed_value == Decimal("0.07")
    # Declared but unobserved: recorded as not observed, never as "not met".
    assert conds[1].observed_value is None and conds[1].condition_met is None
    capsule = json.loads(BASELINE.read_text())
    out = attach_facet(capsule, build_facet(coverage_trigger=record))
    jsonschema.validate(out, json.loads(SCHEMA_PATH.read_text()))


def test_golden_coverage_decision_keys_are_rejected() -> None:
    with pytest.raises(DeterminationFieldRejectedError):
        CoverageTrigger.model_validate(_load("coverage-trigger-invalid-decision.json"))


def test_no_matched_exclusion_is_an_empty_list_not_a_decision() -> None:
    record = _coverage(declared_exclusions=[{"exclusion_id": "X", "fact_markers": ["nope"]}])
    assert record.matched_exclusions == []
    assert not {"covered", "policy_responds", "claim_decision"} & set(CoverageTrigger.model_fields)


def test_coverage_without_parametric_terms_omits_conditions() -> None:
    record = _coverage(parametric_terms=[], observations=None)
    assert record.parametric_conditions is None
    assert "parametric_conditions" not in record.model_dump(exclude_none=True)


def test_duplicate_fact_refs_are_deduplicated() -> None:
    ref = digest_artifact("f")
    record = _coverage(
        trigger_facts=[
            {"fact_ref": ref, "marker": "generative_ai_output"},
            {"fact_ref": ref, "marker": "generative_ai_output"},
        ]
    )
    assert record.trigger_facts == [ref]
    assert record.matched_exclusions[0].fact_refs == [ref]


def test_matched_exclusion_must_cite_recorded_facts() -> None:
    body = _coverage().model_dump(mode="json")
    body["matched_exclusions"][0]["fact_refs"] = [digest_artifact("forged")]
    with pytest.raises(InconsistentComparisonError):
        CoverageTrigger.model_validate(body)
    body["matched_exclusions"][0]["fact_refs"] = []
    with pytest.raises(InconsistentComparisonError):
        CoverageTrigger.model_validate(body)


def test_forged_condition_met_is_rejected() -> None:
    with pytest.raises(InconsistentComparisonError):
        ParametricCondition(
            metric="error_rate",
            operator="gte",
            threshold="0.05",
            observed_value="0.01",
            condition_met=True,
        )
    with pytest.raises(InconsistentComparisonError):
        ParametricCondition(metric="m", operator="gte", threshold="1", condition_met=False)


def test_fact_marker_and_ref_validation() -> None:
    with pytest.raises(InvalidReferenceError):
        _coverage(trigger_facts=[{"fact_ref": "span-123", "marker": "x"}])
    with pytest.raises(ValidationError):
        _coverage(trigger_facts=[{"fact_ref": digest_artifact("f"), "marker": "Bad Marker"}])
    with pytest.raises(ValidationError):
        _coverage(trigger_facts=[{"fact_ref": digest_artifact("f"), "marker": "x", "raw": "t"}])


def test_exclusion_declarations_are_validated() -> None:
    with pytest.raises(ValidationError):
        _coverage(declared_exclusions=[{"exclusion_id": "CG 40 47", "fact_markers": []}])
    with pytest.raises(ValidationError):
        _coverage(declared_exclusions=[{"exclusion_id": "<script>", "fact_markers": ["x"]}])


def test_exclusion_set_ref_must_be_digest() -> None:
    with pytest.raises(InvalidReferenceError):
        _coverage(exclusion_set_ref="CG 40 47")


def test_covered_event_kind_label() -> None:
    with pytest.raises(ValidationError):
        _coverage(covered_event_kind="Erroneous output!")


def test_coverage_inputs_are_bounded() -> None:
    many = [
        {"fact_ref": digest_artifact(str(i)), "marker": "m"} for i in range(MAX_TRIGGER_FACTS + 1)
    ]
    with pytest.raises(UnboundedFieldError):
        _coverage(trigger_facts=many)
    excl = [{"exclusion_id": f"E{i}", "fact_markers": ["m"]} for i in range(MAX_EXCLUSIONS + 1)]
    with pytest.raises(UnboundedFieldError):
        _coverage(declared_exclusions=excl)
    terms = [
        {"metric": f"m{i}", "operator": "gt", "threshold": "1"} for i in range(MAX_CONDITIONS + 1)
    ]
    with pytest.raises(UnboundedFieldError):
        _coverage(parametric_terms=terms)


def test_model_level_caps() -> None:
    ref = digest_artifact("f")
    with pytest.raises(UnboundedFieldError):
        MatchedExclusion(exclusion_id="E", matched_markers=["m"] * 40, fact_refs=[ref])
    body = _coverage().model_dump(mode="json")
    body["matched_exclusions"] = body["matched_exclusions"] * (MAX_EXCLUSIONS + 1)
    with pytest.raises((UnboundedFieldError, DeterminationFieldRejectedError)):
        CoverageTrigger.model_validate(body)
    body = _coverage().model_dump(mode="json")
    body["parametric_conditions"] = body["parametric_conditions"][:1] * (MAX_CONDITIONS + 1)
    with pytest.raises(UnboundedFieldError):
        CoverageTrigger.model_validate(body)


def test_float_observation_is_refused() -> None:
    with pytest.raises(InvalidDecimalError):
        _coverage(observations={"error_rate": 0.07})


def test_parametric_unit_is_label_checked() -> None:
    with pytest.raises(ValidationError):
        _coverage(
            parametric_terms=[{"metric": "m", "operator": "gt", "threshold": "1", "unit": "!"}]
        )


def test_all_three_p2_objects_coexist_in_one_facet() -> None:
    from novafabric.risk_transfer import build_liability_chain

    chain = build_liability_chain(
        [{"role": "agent", "party_ref": digest_artifact("a"), "contribution_marker": "none"}]
    )
    facet = build_facet(liability_chain=chain, sla_breach=_sla(), coverage_trigger=_coverage())
    assert facet is not None
    capsule = json.loads(BASELINE.read_text())
    out = attach_facet(capsule, facet)
    jsonschema.validate(out, json.loads(SCHEMA_PATH.read_text()))
    assert set(out["facets"]["risk_transfer"]) >= {
        "liability_chain",
        "sla_breach",
        "coverage_trigger",
    }
