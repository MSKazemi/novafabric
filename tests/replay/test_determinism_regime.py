"""Determinism regime — could this replay have been expected to match?

The load-bearing property is that **`unknown` is not `eligible`**. An assessment
that answers "probably fine" when the run recorded nothing is worse than no
assessment, because it converts an absence of evidence into evidence.

The second is that the facts travel with the verdict: a caller who accepts
temperature-0 without a pinned seed must be able to reach that conclusion from
the record, rather than being stuck with this module's stricter policy.
"""

from __future__ import annotations

import pytest

from novafabric.replay.determinism import (
    Eligibility,
    assess,
    call_regime,
)


def _call(temp=None, seed=None, *, semconv: bool = False, nested: bool = False):
    body: dict = {}
    if temp is not None:
        body["gen_ai.request.temperature" if semconv else "temperature"] = temp
    if seed is not None:
        body["gen_ai.request.seed" if semconv else "seed"] = seed
    return {"request": body} if nested else body


# ── unknown is never eligible ────────────────────────────────────────────────


def test_a_run_with_no_model_calls_is_unknown_not_eligible() -> None:
    """'Nothing was non-deterministic because nothing happened' is the wrong answer."""
    result = assess([])

    assert result.eligibility is Eligibility.unknown
    assert result.calls == 0
    assert "not the same as a deterministic run" in " ".join(result.reasons)


def test_calls_without_a_recorded_temperature_are_unknown() -> None:
    result = assess([_call(seed=1), _call(seed=2)])

    assert result.eligibility is Eligibility.unknown
    assert result.calls_without_temperature == 2
    assert "not evidence of determinism" in " ".join(result.reasons)


def test_unknown_never_serialises_as_eligible() -> None:
    payload = assess([]).model_dump(mode="json")
    assert payload["eligibility"] == "unknown"
    assert payload["eligibility"] != "eligible"


# ── the verdict ──────────────────────────────────────────────────────────────


def test_fully_pinned_calls_are_eligible() -> None:
    result = assess([_call(0, 42), _call(0.0, 7)])

    assert result.eligibility is Eligibility.eligible
    assert result.calls == 2
    assert result.calls_with_nonzero_temperature == 0
    assert result.calls_without_seed == 0


def test_a_non_zero_temperature_is_not_eligible() -> None:
    result = assess([_call(0, 1), _call(1.2, 2)])

    assert result.eligibility is Eligibility.not_eligible
    assert result.calls_with_nonzero_temperature == 1
    assert "not expected to match" in " ".join(result.reasons)


def test_temperature_zero_without_a_seed_is_not_eligible_by_default() -> None:
    """The schema treats an unpinned seed as non-deterministic."""
    result = assess([_call(0)])

    assert result.eligibility is Eligibility.not_eligible
    assert result.calls_without_seed == 1


def test_a_non_zero_temperature_outranks_a_missing_one() -> None:
    """A known bad regime is a stronger statement than an unknown one."""
    result = assess([_call(0.9, 1), _call(seed=2)])

    assert result.eligibility is Eligibility.not_eligible


# ── the facts travel with the verdict ────────────────────────────────────────


def test_a_caller_can_apply_a_looser_policy_from_the_record() -> None:
    """Temperature-0-without-a-seed: this module says not-eligible; the caller may differ."""
    result = assess([_call(0), _call(0)])

    assert result.eligibility is Eligibility.not_eligible
    # ...but the evidence for the looser reading is right there:
    assert result.calls_with_nonzero_temperature == 0
    assert result.calls_without_temperature == 0
    assert result.temperatures == [0.0]


def test_reasons_are_enumerated_not_a_bare_boolean() -> None:
    for result in (assess([]), assess([_call(0)]), assess([_call(2.0, 1)])):
        assert result.reasons, "a verdict without a reason cannot be argued with"


def test_per_call_detail_is_reported() -> None:
    result = assess([_call(0, 1), _call(0.7)])

    assert [c.index for c in result.per_call] == [0, 1]
    assert result.per_call[0].seed_pinned is True
    assert result.per_call[1].temperature == 0.7
    assert result.per_call[1].seed_pinned is False


# ── both capture shapes ──────────────────────────────────────────────────────


@pytest.mark.parametrize("semconv", [False, True], ids=["body-key", "semconv"])
@pytest.mark.parametrize("nested", [False, True], ids=["flat", "nested-request"])
def test_both_capture_shapes_are_read(semconv: bool, nested: bool) -> None:
    """Capture writes plain body keys or OTel semconv attributes, flat or nested."""
    result = assess([_call(0, 42, semconv=semconv, nested=nested)])

    assert result.eligibility is Eligibility.eligible, (
        f"semconv={semconv} nested={nested} was not read; reading only one shape "
        "would silently report unknown for half the capture paths"
    )


def test_a_boolean_is_not_a_temperature() -> None:
    """`True` is an int in Python; it is not a temperature of 1."""
    regime = call_regime({"temperature": True, "seed": True}, 0)

    assert regime.temperature is None
    assert regime.seed_pinned is False


# ── relationship to the shipped classifier ───────────────────────────────────


def test_the_shipped_determinism_classifier_still_exists() -> None:
    """This module complements `evidence/replay_attestation.py`; it does not replace it.

    That classifier (ADR-0094 B, `nova replay --certify`) answers "was this replay
    reproducible?" from an executed replay and outcome digests, reading
    `model_digest` / `seed` / `lock_mode`. This module answers the earlier
    question — "was the original run's *sampling* regime deterministic?" — from
    the capsule alone, and reads temperature, which the classifier does not.

    If the classifier ever grows temperature awareness, these two overlap and one
    should absorb the other. This test exists so that becomes a decision rather
    than a silent duplication.
    """
    from novafabric.evidence.replay_attestation import (
        _is_fully_pinned,
        classify_determinism,
        pinned_block_from_capsule,
    )

    assert callable(classify_determinism)
    assert callable(pinned_block_from_capsule)

    import inspect

    source = inspect.getsource(_is_fully_pinned)
    assert "temperature" not in source, (
        "the shipped classifier now considers temperature, which is what "
        "replay/determinism.py was written to add. Reconcile them — folding one "
        "into the other changes what a signed ReplayAttestation asserts, so it "
        "needs an ADR, but they must not both quietly assess the same thing."
    )
