"""NF-168 sealed output-conformance (ADR-0148 D2).

The load-bearing test is `test_tampering_with_a_recorded_verdict_breaks_the_seal`, paired with
`test_an_untouched_record_verifies` — a seal that fails for everything proves as little as one
that passes for everything, so both directions are asserted.
"""

from __future__ import annotations

import pytest

from novafabric.supplychain.toolschema.conformance_seal import (
    FACET_NAME,
    PREDICATE_KEY,
    ConformanceSealError,
    attach_facet,
    digest_of,
    facet_from_capsule,
    into_predicate,
    seal_conformance,
    verify_seal,
)


def _verdict(*, arguments_valid=True, result_valid=None, errors=None) -> dict:
    return {
        "arguments_valid": arguments_valid,
        "result_valid": result_valid,
        "validator": "novafabric/jsonschema@1",
        "errors": errors or [],
        "checked_at": "2026-07-12T10:00:00Z",
        "arguments_schema_ref": "schemas/args.json",
        "result_schema_ref": None,
    }


VERDICTS = [_verdict(), _verdict(arguments_valid=False), None]


# ── The seal must be falsifiable, in both directions ─────────────────────


def test_an_untouched_record_verifies() -> None:
    seal = seal_conformance(VERDICTS)
    assert verify_seal(seal.verdicts, seal.sealed_digest) is True


def test_tampering_with_a_recorded_verdict_breaks_the_seal() -> None:
    """Flipping a failure to a pass is exactly what the seal exists to catch."""
    seal = seal_conformance(VERDICTS)
    tampered = [dict(v) if v else None for v in seal.verdicts]
    assert tampered[1] is not None
    tampered[1]["arguments_valid"] = True

    assert verify_seal(tampered, seal.sealed_digest) is False


def test_deleting_a_verdict_breaks_the_seal() -> None:
    """Dropping a failing call would otherwise be a silent way to clean a record."""
    seal = seal_conformance(VERDICTS)
    assert verify_seal(seal.verdicts[:-1], seal.sealed_digest) is False


def test_reordering_the_verdicts_breaks_the_seal() -> None:
    """The verdicts describe a sequence of calls; reordering describes a different run."""
    seal = seal_conformance(VERDICTS)
    reordered = [seal.verdicts[1], seal.verdicts[0], seal.verdicts[2]]
    assert verify_seal(reordered, seal.sealed_digest) is False


def test_the_digest_is_deterministic_for_the_same_verdicts() -> None:
    assert digest_of(VERDICTS) == digest_of([dict(v) if v else None for v in VERDICTS])


# ── The predicate must not carry the verdicts ────────────────────────────


def test_the_predicate_carries_the_digest_not_the_verdicts() -> None:
    """Keeping them apart is what leaves verification two independent sources."""
    predicate = into_predicate(seal_conformance(VERDICTS))
    body = predicate[PREDICATE_KEY]

    assert body["sealed_digest"].startswith("sha256:")
    assert "verdicts" not in body
    assert "arguments_valid" not in str(body), "no verdict content leaked into the predicate"


def test_the_predicate_carries_the_counts() -> None:
    body = into_predicate(seal_conformance(VERDICTS))[PREDICATE_KEY]
    assert (body["calls"], body["conforming"], body["violating"], body["unchecked"]) == (
        3, 1, 1, 1,
    )


def test_the_predicate_key_is_versioned() -> None:
    """A change to the digest construction must not be mistakable for the same claim."""
    assert PREDICATE_KEY.endswith("/v1")


# ── "Not checked" is not "conforming" ────────────────────────────────────


def test_an_unchecked_call_is_not_counted_as_conforming() -> None:
    seal = seal_conformance([None, None])
    assert (seal.conforming, seal.violating, seal.unchecked) == (0, 0, 2)


def test_a_capsule_where_nothing_declared_a_schema_claims_no_conformance() -> None:
    """Otherwise it would report perfect conformance having validated nothing."""
    seal = seal_conformance([None, None, None])
    assert seal.conforming == 0
    assert seal.calls == 3


def test_a_result_failure_counts_as_a_violation() -> None:
    seal = seal_conformance([_verdict(arguments_valid=True, result_valid=False)])
    assert (seal.conforming, seal.violating) == (0, 1)


def test_a_none_result_is_not_a_violation() -> None:
    """`result_valid` is None when the tool errored — skipped, not failed (ADR-0128)."""
    seal = seal_conformance([_verdict(arguments_valid=True, result_valid=None)])
    assert (seal.conforming, seal.violating) == (1, 0)


# ── Refusals ─────────────────────────────────────────────────────────────


def test_sealing_nothing_is_refused() -> None:
    """A digest over an empty list is a constant — every empty capsule would compare equal."""
    with pytest.raises(ConformanceSealError, match="no tool calls to seal"):
        seal_conformance([])


def test_the_seal_carries_no_verdict_field() -> None:
    fields = set(seal_conformance(VERDICTS).model_dump().keys())
    assert not fields & {"passed", "ok", "verdict", "blocked", "compliant"}


# ── The verdicts are reused verbatim ─────────────────────────────────────


def test_the_recorded_verdict_is_not_reinterpreted() -> None:
    """D2's reuse rule: this module hashes and counts, it does not re-validate."""
    seal = seal_conformance(VERDICTS)
    assert seal.verdicts[0] == VERDICTS[0]
    assert seal.verdicts[1] == VERDICTS[1]
    assert seal.verdicts[2] is None


# ── Facet ────────────────────────────────────────────────────────────────


def test_the_facet_round_trips_and_is_additive() -> None:
    seal = seal_conformance(VERDICTS)
    capsule: dict = {"run_id": "r1"}
    attached = attach_facet(capsule, seal)

    assert capsule == {"run_id": "r1"}
    assert set(attached["facets"]) == {FACET_NAME}
    read_back = facet_from_capsule(attached)
    assert read_back is not None and read_back.sealed_digest == seal.sealed_digest


def test_a_seal_survives_the_facet_round_trip_and_still_verifies() -> None:
    """The facet is the source verification reads from, so the trip must not alter it."""
    seal = seal_conformance(VERDICTS)
    read_back = facet_from_capsule(attach_facet({}, seal))
    assert read_back is not None
    assert verify_seal(read_back.verdicts, seal.sealed_digest) is True


def test_attaching_nothing_leaves_the_capsule_byte_identical() -> None:
    capsule = {"run_id": "r1"}
    assert attach_facet(capsule, None) == capsule


def test_an_invalid_facet_is_reported_not_silently_dropped() -> None:
    with pytest.raises(ConformanceSealError, match=f"invalid {FACET_NAME} facet"):
        facet_from_capsule({"facets": {FACET_NAME: {"calls": "many"}}})


def test_a_capsule_without_the_facet_reads_as_none() -> None:
    assert facet_from_capsule({"run_id": "r1"}) is None
    assert facet_from_capsule({"facets": {}}) is None
