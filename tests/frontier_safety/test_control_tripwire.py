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

"""ADR-0167 P2 — control-protocol decision (NF-352) + tripwire trigger (NF-357).

Organised by the three things the ADR's P2 line demands: governed-action
linkage, the no-block / fail-open assertion, and the C4
``guardrail_decision_ref`` boundary — plus the I-3 / I-5 invariants every
frontier-safety object inherits.
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
    CommitmentBinding,
    ComputedVerdictError,
    ControlDecision,
    FrontierSafetyFacet,
    GuardrailDuplicationError,
    InvalidReferenceError,
    PayloadCaptureError,
    ThresholdEval,
    TripwireTrigger,
    attach_facet,
    build_facet,
    decisions_for_action,
    digest_ref,
    facet_from_capsule,
    guardrail_decision_digest,
    record_control_decision,
    record_tripwire_trigger,
    verify_governed_action,
    verify_guardrail_link,
    verify_tripwire_commitment,
)
from novafabric.frontier_safety._common import MAX_EXTRA_DEPTH
from novafabric.safety.decisions import GuardrailDecision

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "run-capsule.schema.json"
BASELINE = REPO_ROOT / "tests" / "fixtures" / "model-provenance" / "valid-text-only-capsule.json"

ACTION = '{"step": 7, "tool": "bash", "args_digest": "sha256:00"}'
OTHER_ACTION = '{"step": 8, "tool": "bash"}'
MONITOR = "trusted-monitor model=small-v2 config=v5"
PROTOCOL_LOG = "ctrl-z decision log entry 7"
COMMITMENT_TEXT = "FSF v3 CCL cyber autonomous uplift: apply security mitigations"
OBSERVATION = "cyber-uplift eval 2026-09 run 3"
SECRET_PROMPT = "SYSTEM: you are the trusted monitor; flag anything touching /etc/shadow"


@pytest.fixture
def capsule() -> dict[str, Any]:
    return json.loads(BASELINE.read_text())


def _decision(**kw: object) -> ControlDecision:
    base: dict[str, object] = {
        "protocol": "resample",
        "decision": "resample",
        "governed_action_ref": digest_ref(ACTION),
        "monitor_ref": digest_ref(MONITOR),
        "verdict_ref": digest_ref(PROTOCOL_LOG),
    }
    base.update(kw)
    return ControlDecision(**base)  # type: ignore[arg-type]


def _trigger(**kw: object) -> TripwireTrigger:
    base: dict[str, object] = {
        "indicator_id": "fsf.ccl.cyber.autonomous-uplift",
        "framework": "deepmind_fsf",
        "commitment_ref": digest_ref(COMMITMENT_TEXT),
        "triggered_by_ref": digest_ref(OBSERVATION),
    }
    base.update(kw)
    return TripwireTrigger(**base)  # type: ignore[arg-type]


def _guardrail(**kw: object) -> GuardrailDecision:
    base: dict[str, object] = {
        "decision_id": "gd-0001",
        "phase": "output",
        "disposition": "block",
        "decided_at": "2026-09-24T10:00:00Z",
    }
    base.update(kw)
    return GuardrailDecision(**base)  # type: ignore[arg-type]


# ── NF-352: control-protocol decision ─────────────────────────────────────


def test_ctrl_z_resample_acceptance() -> None:
    """Spec §6 'Control decision (NF-352)' Given-When-Then."""
    d = _decision()
    assert d.protocol == "resample"
    assert d.decision == "resample"
    assert d.governed_action_ref == digest_ref(ACTION)
    assert d.verdict_source == "control_protocol"
    assert d.verdict is None


@pytest.mark.parametrize(
    "protocol",
    [
        "trusted_monitor",
        "untrusted_monitor",
        "resample",
        "defer_to_trusted",
        "defer_to_human",
        "interrogate",
        "other",
    ],
)
def test_every_spec_protocol_is_accepted(protocol: str) -> None:
    assert _decision(protocol=protocol).protocol == protocol


@pytest.mark.parametrize("decision", ["accept", "edit", "resample", "defer", "audit"])
def test_every_spec_decision_is_accepted(decision: str) -> None:
    assert _decision(decision=decision).decision == decision


@pytest.mark.parametrize(
    ("field", "value"),
    [("protocol", "novafabric_monitor"), ("decision", "block"), ("decision", "halt")],
)
def test_unknown_protocol_or_decision_is_rejected(field: str, value: str) -> None:
    """`block`/`halt` are not outcomes NovaFabric can even spell (I-1)."""
    with pytest.raises(ValidationError):
        _decision(**{field: value})


def test_verdict_ref_is_required_for_a_control_decision() -> None:
    """A decision is only recordable together with the protocol's own log entry."""
    with pytest.raises(ValidationError):
        ControlDecision(
            protocol="resample",
            decision="resample",
            governed_action_ref=digest_ref(ACTION),
            monitor_ref=digest_ref(MONITOR),
        )  # type: ignore[call-arg]


@pytest.mark.parametrize("source", ["red_team", "human_decision", "novafabric"])
def test_verdict_source_can_only_be_the_control_protocol(source: str) -> None:
    with pytest.raises(ValidationError):
        _decision(verdict_source=source)


def test_attributed_verdict_value_is_allowed() -> None:
    """A verdict value is fine when it quotes the protocol by reference."""
    d = _decision(verdict="suspicious")
    assert d.is_evaluated is True


def test_governed_action_ref_must_be_a_content_digest() -> None:
    with pytest.raises(InvalidReferenceError):
        _decision(governed_action_ref="https://example.org/action/7")


def test_monitor_ref_bytes_are_a_payload_not_a_ref() -> None:
    with pytest.raises(PayloadCaptureError):
        _decision(monitor_ref=SECRET_PROMPT.encode())


def test_monitor_prompt_is_never_captured() -> None:
    """Spec §3.5: monitor_ref identifies the monitor, never its prompt."""
    with pytest.raises(PayloadCaptureError, match="monitor_prompt"):
        _decision(monitor_prompt=SECRET_PROMPT)


def test_nested_payload_in_extra_fields_is_rejected() -> None:
    with pytest.raises(PayloadCaptureError, match="transcript"):
        _decision(context={"notes": [{"Transcript": "…"}]})


def test_unbounded_nesting_in_extra_fields_is_rejected() -> None:
    deep: dict[str, Any] = {}
    cursor = deep
    for _ in range(MAX_EXTRA_DEPTH + 2):
        cursor["k"] = {}
        cursor = cursor["k"]
    with pytest.raises(PayloadCaptureError, match="nest deeper"):
        _decision(context=deep)


def test_harmless_extra_fields_are_kept() -> None:
    """extra="allow" stays additive: an unknown reference field survives."""
    d = _decision(protocol_version="ctrl-z/1.2")
    assert d.model_dump()["protocol_version"] == "ctrl-z/1.2"


# ── Governed-action linkage ───────────────────────────────────────────────


def test_governed_action_verifies_against_the_action_record() -> None:
    d = _decision()
    assert verify_governed_action(d, ACTION) is True
    assert verify_governed_action(d, OTHER_ACTION) is False


def test_decisions_for_action_filters_and_preserves_order() -> None:
    first = _decision(decision="resample")
    second = _decision(decision="accept")
    other = _decision(governed_action_ref=digest_ref(OTHER_ACTION), decision="audit")
    got = decisions_for_action([first, other, second], digest_ref(ACTION))
    assert got == (first, second)
    assert decisions_for_action([first], digest_ref(OTHER_ACTION)) == ()


# ── NF-357: tripwire trigger ──────────────────────────────────────────────


def test_spec_4_2_tripwire_example_validates() -> None:
    t = _trigger(verdict_ref=digest_ref("fsf evaluator ruling"))
    assert t.fired is True
    assert t.verdict is None
    assert t.framework == "deepmind_fsf"


def test_a_tripwire_that_did_not_fire_is_unrepresentable() -> None:
    """Absence is recorded by no object (I-2), never by `fired: false`."""
    with pytest.raises(ValidationError):
        _trigger(fired=False)


def test_blank_indicator_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _trigger(indicator_id="   ")


def test_commitment_ref_must_be_a_digest() -> None:
    with pytest.raises(InvalidReferenceError):
        _trigger(commitment_ref="https://deepmind.google/fsf#ccl-cyber")


@pytest.mark.parametrize("verdict", [True, False, "exceeded"])
def test_unattributed_tripwire_verdict_is_a_computed_verdict(verdict: object) -> None:
    """I-3: a bare verdict — including False — is NovaFabric adjudicating."""
    with pytest.raises(ComputedVerdictError):
        _trigger(verdict=verdict)


def test_attributed_tripwire_verdict_is_allowed() -> None:
    t = _trigger(
        verdict="ccl_reached",
        verdict_ref=digest_ref("ruling"),
        verdict_source="fsf_evaluator",
    )
    assert t.is_evaluated is True


def test_tripwire_payload_is_rejected() -> None:
    with pytest.raises(PayloadCaptureError):
        _trigger(exploit_steps=["step 1"])


def test_tripwire_binds_to_the_commitment_it_implicates() -> None:
    """Spec §6 'Tripwire→commitment (NF-357/353)'."""
    binding = CommitmentBinding(
        framework="deepmind_fsf",
        commitment_id="fsf.v3.ccl.cyber",
        commitment_digest=digest_ref(COMMITMENT_TEXT),
        subject_type="incident",
        implicated_by_ref=digest_ref(OBSERVATION),
    )
    other = binding.model_copy(update={"commitment_digest": digest_ref("other text")})
    t = _trigger()
    assert verify_tripwire_commitment(t, binding) is True
    assert verify_tripwire_commitment(t, other) is False


# ── C4 boundary: guardrail_decision_ref ───────────────────────────────────


def test_straddling_decision_references_the_c4_object_by_digest() -> None:
    """Spec §6 'No-duplication (C4 boundary)'."""
    gd = _guardrail()
    d = _decision(guardrail_decision_ref=guardrail_decision_digest(gd))
    assert verify_guardrail_link(d, gd) is True
    assert verify_guardrail_link(d, _guardrail(decision_id="gd-0002")) is False
    # The C4 fields are not re-recorded on the frontier-safety object.
    dumped = d.model_dump(exclude_none=True)
    assert "disposition" not in dumped
    assert "phase" not in dumped


def test_guardrail_digest_is_canonical() -> None:
    """Same decision → same digest, whatever the key order or null fields."""
    gd = _guardrail()
    as_map = gd.model_dump(mode="json")
    reordered = dict(reversed(list(as_map.items())))
    assert guardrail_decision_digest(gd) == guardrail_decision_digest(reordered)
    assert guardrail_decision_digest(gd).startswith("sha256:")


def test_missing_guardrail_link_is_false_not_an_error() -> None:
    assert verify_guardrail_link(_trigger(), _guardrail()) is False


@pytest.mark.parametrize(
    "extra",
    [
        {"disposition": "block"},
        {"decision_inputs_digest": digest_ref("x")},
        {"guardrail_decision": {"decision_id": "gd-0001"}},
    ],
)
def test_inlined_c4_decision_is_rejected(extra: dict[str, Any]) -> None:
    with pytest.raises(GuardrailDuplicationError, match="guardrail_decision_ref"):
        _decision(**extra)
    with pytest.raises(GuardrailDuplicationError):
        _trigger(**extra)


def test_guardrail_decision_ref_must_be_a_digest() -> None:
    with pytest.raises(InvalidReferenceError):
        _decision(guardrail_decision_ref="https://c4.example/gd-0001")
    with pytest.raises(InvalidReferenceError):
        _trigger(guardrail_decision_ref="gd-0001")


# ── Facet integration (I-2 additive) ──────────────────────────────────────


def test_spec_wire_shape_singular_members_validate(capsule: dict[str, Any]) -> None:
    capsule["facets"] = {
        FACET_NAME: {
            "control_decision": _decision().model_dump(mode="json"),
            "tripwire_trigger": _trigger().model_dump(mode="json"),
        }
    }
    facet = facet_from_capsule(capsule)
    assert facet is not None
    assert len(facet.all_control_decisions()) == 1
    assert len(facet.all_tripwire_triggers()) == 1
    assert facet.has_material is True


def test_all_members_merge_singular_and_plural() -> None:
    a, b = _decision(), _decision(decision="accept")
    facet = FrontierSafetyFacet(control_decision=a, control_decisions=[b])
    assert facet.all_control_decisions() == (a, b)
    t1, t2 = _trigger(), _trigger(indicator_id="asl-4.autonomy")
    facet = FrontierSafetyFacet(tripwire_trigger=t1, tripwire_triggers=[t2])
    assert facet.all_tripwire_triggers() == (t1, t2)


def test_empty_lists_are_not_material(capsule: dict[str, Any]) -> None:
    facet = build_facet(control_decisions=[], tripwire_triggers=[])
    assert facet.has_material is False
    assert attach_facet(capsule, facet) is capsule
    assert FrontierSafetyFacet(control_decisions=[]).has_material is False


def test_p2_facet_round_trips_and_validates_against_the_capsule_schema(
    capsule: dict[str, Any],
) -> None:
    facet = build_facet(control_decisions=[_decision()], tripwire_triggers=[_trigger()])
    out = attach_facet(capsule, facet)
    jsonschema.validate(out, json.loads(SCHEMA_PATH.read_text()))
    block = out["facets"][FACET_NAME]
    # verdict: null is absent after serialisation (absent == not evaluated).
    assert "verdict" not in block["control_decisions"][0]
    back = facet_from_capsule(json.loads(json.dumps(out)))
    assert back is not None
    assert back.all_control_decisions() == (_decision(),)
    assert back.all_tripwire_triggers() == (_trigger(),)


def test_p1_facet_is_still_a_valid_p2_facet() -> None:
    p1 = {
        "schema_version": "0.1.0",
        "threshold_eval": {
            "framework": "anthropic_rsp",
            "framework_version": "3.0",
            "threshold_id": "ASL-3",
            "eval_ran": True,
            "eval_ref": digest_ref("eval"),
        },
    }
    facet = FrontierSafetyFacet.model_validate(p1)
    assert facet.all_control_decisions() == ()
    assert facet.all_tripwire_triggers() == ()


# ── No-block / fail-open (I-1, I-2) ───────────────────────────────────────


def test_record_control_decision_appends(capsule: dict[str, Any]) -> None:
    out = record_control_decision(capsule, _decision())
    out = record_control_decision(out, _decision(decision="accept").model_dump())
    facet = facet_from_capsule(out)
    assert facet is not None
    assert [d.decision for d in facet.all_control_decisions()] == ["resample", "accept"]
    assert FACET_NAME not in (capsule.get("facets") or {})  # input not mutated


def test_record_preserves_existing_p1_material(capsule: dict[str, Any]) -> None:
    te = ThresholdEval(
        framework="anthropic_rsp",
        framework_version="3.0",
        threshold_id="ASL-3",
        eval_ref=digest_ref("eval"),
    )
    base = attach_facet(capsule, build_facet(threshold_eval=te))
    out = record_tripwire_trigger(base, _trigger())
    facet = facet_from_capsule(out)
    assert facet is not None
    assert facet.threshold_eval == te
    assert facet.all_tripwire_triggers() == (_trigger(),)


def test_a_fired_tripwire_and_a_defer_decision_block_nothing(
    capsule: dict[str, Any],
) -> None:
    """The no-block assertion: recording returns normally, run continues.

    The most alarming material P2 can carry — a fired autonomy indicator and a
    protocol decision to defer to a human — produces a capsule dict and
    nothing else: no exception, no exit, no status change on the run.
    """
    status_before = capsule.get("status")
    out = record_tripwire_trigger(capsule, _trigger(indicator_id="asl-4.autonomy"))
    out = record_control_decision(out, _decision(protocol="defer_to_human", decision="defer"))
    assert isinstance(out, dict)
    assert out.get("status") == status_before


@pytest.mark.parametrize(
    "bad",
    [
        {"protocol": "resample"},  # missing required fields
        {
            "protocol": "resample",
            "decision": "resample",
            "governed_action_ref": ACTION.encode(),  # raw bytes → payload
            "monitor_ref": digest_ref(MONITOR),
            "verdict_ref": digest_ref(PROTOCOL_LOG),
        },
        {
            "protocol": "trusted_monitor",
            "decision": "audit",
            "governed_action_ref": digest_ref(ACTION),
            "monitor_ref": digest_ref(MONITOR),
            "verdict_ref": digest_ref(PROTOCOL_LOG),
            "monitor_prompt": SECRET_PROMPT,
        },
        {
            "protocol": "trusted_monitor",
            "decision": "audit",
            "governed_action_ref": digest_ref(ACTION),
            "monitor_ref": digest_ref(MONITOR),
            "verdict_ref": digest_ref(PROTOCOL_LOG),
            "disposition": "block",
        },
        42,  # not a mapping at all
    ],
)
def test_invalid_control_material_is_dropped_fail_open(
    capsule: dict[str, Any], bad: Any, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="novafabric.frontier_safety.record"):
        out = record_control_decision(capsule, bad)
    assert out is capsule
    assert any("control_decision.dropped" in r.getMessage() for r in caplog.records)
    # The dropped values never reach the log (I-5).
    for record in caplog.records:
        assert SECRET_PROMPT not in record.getMessage()
        assert SECRET_PROMPT not in str(record.__dict__)


def test_invalid_tripwire_material_is_dropped_fail_open(
    capsule: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="novafabric.frontier_safety.record"):
        out = record_tripwire_trigger(capsule, {"indicator_id": "x", "fired": False})
    assert out is capsule
    assert any("tripwire_trigger.dropped" in r.getMessage() for r in caplog.records)


def test_malformed_existing_facet_does_not_block_recording(
    capsule: dict[str, Any],
) -> None:
    capsule["facets"] = {FACET_NAME: {"threshold_eval": {"framework": "nope"}}}
    assert record_control_decision(capsule, _decision()) is capsule
    assert record_tripwire_trigger(capsule, _trigger()) is capsule
