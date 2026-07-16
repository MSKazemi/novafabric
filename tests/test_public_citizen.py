"""ADR-0169 D1 / NF-377 — citizen-facing decision-explanation export.

A plain-language, subject-facing record of *meaningful information* about a decision — ``decision_ref``,
the recorded non-secret ``factors``, ``human_involvement`` (a three-value enum), ``contest_channel_ref``,
and ``logic_summary_ref``. It records meaningful info (Art. 22 + Arts. 13–15 shape); it **MUST NOT**
claim legal sufficiency and **MUST NOT** expose model internals (weights/logits/activations/…) or raw
sensitive identifiers.
"""
from __future__ import annotations

import pytest

from novafabric.compliance.export.public._citizen import (
    CitizenExplanation,
    build_citizen_explanation,
    disallowed_factor_content,
)


def test_valid_record():
    rec = build_citizen_explanation(
        decision_ref="capsule://root#dec",
        factors=["income below the assistance threshold", "no prior claim in 12 months"],
        human_involvement="human_in_the_loop",
        contest_channel_ref="https://appeal.example.gov/case",
        logic_summary_ref="doc://logic-summary#v3",
    )
    assert isinstance(rec, CitizenExplanation)
    assert rec.decision_ref == "capsule://root#dec"
    assert rec.human_involvement == "human_in_the_loop"
    assert len(rec.factors) == 2


@pytest.mark.parametrize(
    "level", ["solely_automated", "human_in_the_loop", "human_reviewed"]
)
def test_all_three_involvement_levels_accepted(level):
    rec = build_citizen_explanation(decision_ref="d", human_involvement=level)
    assert rec.human_involvement == level


def test_invalid_involvement_rejected():
    with pytest.raises(ValueError):
        build_citizen_explanation(decision_ref="d", human_involvement="fully_manual")


@pytest.mark.parametrize(
    "bad_factor",
    [
        "layer_3 activation was 0.87",
        "output logit 2.1 for class A",
        "embedding cosine distance 0.4",
        "gradient norm exceeded",
        "softmax over the vocabulary",
        "applicant SSN on file",
    ],
)
def test_model_internals_and_raw_identifiers_are_rejected(bad_factor):
    with pytest.raises(ValueError) as exc:
        build_citizen_explanation(
            decision_ref="d", factors=[bad_factor], human_involvement="human_reviewed"
        )
    assert "factor" in str(exc.value).lower()


def test_disallowed_factor_content_flags_offenders():
    flagged = disallowed_factor_content(
        ["income below threshold", "hidden_state vector", "passport_number recorded"]
    )
    assert "hidden_state vector" in flagged
    assert "passport_number recorded" in flagged
    assert "income below threshold" not in flagged


def test_missing_required_raises():
    with pytest.raises(ValueError):
        build_citizen_explanation(decision_ref="d")  # no human_involvement
    with pytest.raises(ValueError):
        build_citizen_explanation(human_involvement="human_reviewed")  # no decision_ref


def test_no_legal_sufficiency_or_model_internals_field():
    for forbidden in ("legally_sufficient", "legal_sufficiency", "verdict", "compliant", "weights"):
        assert forbidden not in CitizenExplanation.model_fields
