"""Canary-run record — ADR-0147 D3 / NF-153 (evidence half).

The fingerprint tests carry the weight. Its job is to answer "was this canary judged
against the same stack as the baseline?", and two of its three required properties
fail *quietly*: a non-deterministic digest makes every canary look like a stack
change, and an order-dependent one makes every canary look like a regression.
"""

from __future__ import annotations

import json

import pytest

from novafabric.assure.canary import (
    CanaryError,
    attach_facet,
    facet_from_capsule,
    record_canary_run,
    stack_fingerprint,
)

STACK = {"model:gpt": "2026-07-18", "tool:search": "1.2.0", "tool:db": "0.9.1"}
RAN_AT = "2026-07-12T00:00:00Z"


# ── the stack fingerprint ────────────────────────────────────────────────────


def test_the_fingerprint_is_deterministic() -> None:
    """A digest that varies per call makes every canary look like a stack change."""
    assert stack_fingerprint(STACK) == stack_fingerprint(dict(STACK))


def test_the_fingerprint_is_order_independent() -> None:
    """A stack is a set. If order mattered, every canary would look like a regression."""
    reordered = dict(reversed(list(STACK.items())))

    assert reordered != STACK or list(reordered) != list(STACK)
    assert stack_fingerprint(reordered) == stack_fingerprint(STACK)


@pytest.mark.parametrize(
    "changed",
    [
        {"model:gpt": "2026-08-01", "tool:search": "1.2.0", "tool:db": "0.9.1"},
        {"model:gpt": "2026-07-18", "tool:search": "1.3.0", "tool:db": "0.9.1"},
        {"model:gpt": "2026-07-18", "tool:search": "1.2.0"},
        {**STACK, "tool:email": "2.0.0"},
    ],
    ids=["model bumped", "tool bumped", "tool removed", "tool added"],
)
def test_the_fingerprint_is_version_sensitive(changed: dict) -> None:
    """Insensitivity would pass silently through the event this exists to catch."""
    assert stack_fingerprint(changed) != stack_fingerprint(STACK)


def test_an_empty_stack_is_refused() -> None:
    """A fingerprint over nothing is a constant — every stack would compare equal."""
    with pytest.raises(CanaryError, match="no components"):
        stack_fingerprint({})


def test_the_fingerprint_is_a_canonical_digest() -> None:
    fp = stack_fingerprint(STACK)
    assert fp.startswith("sha256:")
    assert len(fp) == len("sha256:") + 64


# ── the record ───────────────────────────────────────────────────────────────


def test_the_record_carries_every_required_field() -> None:
    run = record_canary_run("bl-1", ran_at=RAN_AT, stack=STACK,
                            equivalent=True, drift_score=0.0)
    payload = run.model_dump(exclude_none=True)

    for field in ("baseline_id", "ran_at", "stack_fingerprint", "verdict",
                  "drift_score", "alarm"):
        assert field in payload, f"spec §5.8 requires {field}"


def test_a_non_equivalent_verdict_raises_the_alarm() -> None:
    run = record_canary_run("bl-1", ran_at=RAN_AT, stack=STACK,
                            equivalent=False, drift_score=0.7)
    assert run.alarm is True


def test_an_equivalent_verdict_does_not_alarm() -> None:
    run = record_canary_run("bl-1", ran_at=RAN_AT, stack=STACK, equivalent=True)
    assert run.alarm is False


def test_the_alarm_cannot_disagree_with_the_verdict() -> None:
    """`alarm` is derived, never supplied, so the two cannot drift apart."""
    with pytest.raises(TypeError):
        record_canary_run(  # type: ignore[call-arg]
            "bl-1", ran_at=RAN_AT, stack=STACK, equivalent=True, alarm=True
        )


# ── cross-stack comparison is visible, not silent ────────────────────────────


def test_a_matching_stack_is_recorded_as_matching() -> None:
    run = record_canary_run("bl-1", ran_at=RAN_AT, stack=STACK,
                            equivalent=True, baseline_stack=STACK)
    assert run.same_stack is True
    assert run.baseline_stack_fingerprint == run.stack_fingerprint


def test_a_changed_stack_is_visible() -> None:
    """A 'regression' that is really a stack change is a false alarm that looks true."""
    newer = {**STACK, "model:gpt": "2026-08-01"}

    run = record_canary_run("bl-1", ran_at=RAN_AT, stack=newer,
                            equivalent=False, baseline_stack=STACK)

    assert run.same_stack is False
    assert run.baseline_stack_fingerprint != run.stack_fingerprint


def test_an_unknown_baseline_stack_is_none_not_matched() -> None:
    """'We don't know' must not read as 'they matched'."""
    run = record_canary_run("bl-1", ran_at=RAN_AT, stack=STACK, equivalent=True)

    assert run.same_stack is None
    assert run.baseline_stack_fingerprint is None


# ── validation ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["yesterday", "2026-13-01T00:00:00Z", ""])
def test_an_unparseable_timestamp_is_refused(bad: str) -> None:
    with pytest.raises(CanaryError, match="RFC 3339"):
        record_canary_run("bl-1", ran_at=bad, stack=STACK, equivalent=True)


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(CanaryError, match="UTC offset"):
        record_canary_run("bl-1", ran_at="2026-07-12T00:00:00", stack=STACK,
                          equivalent=True)


def test_an_empty_baseline_id_is_refused() -> None:
    with pytest.raises(CanaryError):
        record_canary_run("  ", ran_at=RAN_AT, stack=STACK, equivalent=True)


def test_offsets_are_normalised_to_utc() -> None:
    run = record_canary_run("bl-1", ran_at="2026-07-12T02:00:00+02:00",
                            stack=STACK, equivalent=True)
    assert run.ran_at == "2026-07-12T00:00:00Z"


# ── facet ────────────────────────────────────────────────────────────────────


def test_no_run_leaves_the_capsule_byte_identical() -> None:
    capsule = {"run_id": "r", "facets": {"other": {"x": 1}}}
    original = json.dumps(capsule, sort_keys=True)

    assert attach_facet(capsule, None) == capsule
    assert json.dumps(capsule, sort_keys=True) == original


def test_round_trip_through_a_capsule() -> None:
    run = record_canary_run("bl-1", ran_at=RAN_AT, stack=STACK, equivalent=False,
                            drift_score=0.4, baseline_stack=STACK)
    assert facet_from_capsule(attach_facet({"run_id": "r"}, run)) == run


def test_an_invalid_record_is_reported() -> None:
    with pytest.raises(CanaryError, match="invalid canary run"):
        facet_from_capsule({"facets": {"canary_run": {"baseline_id": "bl"}}})
