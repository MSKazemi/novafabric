"""Continuous-assurance attestation — ADR-0147 D7 / NF-159.

The requirement that shapes these tests is "a missed run is itself detectable".
A missed run writes nothing, so the only way absence becomes evidence is that each
success states when the next run is due. The tests therefore focus on `next_due`
being derived rather than claimed, and on the boundary where "due" becomes "late".
"""

from __future__ import annotations

import json

import pytest

from novafabric.assure.attestation import (
    PREDICATE_KEY,
    AssuranceAttestation,
    AttestationError,
    attach_facet,
    check_overdue,
    facet_from_capsule,
    into_predicate,
    record_run,
)

RAN_AT = "2026-07-12T00:00:00Z"
DAY = 86400


@pytest.fixture()
def attestation() -> AssuranceAttestation:
    return record_run(
        "nightly-assurance",
        ran_at=RAN_AT,
        cadence_seconds=DAY,
        baselines_checked=["bl-support-agent-golden-2026Q2"],
        detectors_run=["output-drift", "silent-failure"],
        alarms_fired=1,
    )


# ── the MUST fields ──────────────────────────────────────────────────────────


def test_it_records_every_required_field(attestation: AssuranceAttestation) -> None:
    payload = attestation.model_dump()
    for field in ("schedule_id", "ran_at", "baselines_checked", "detectors_run",
                  "alarms_fired", "next_due"):
        assert field in payload, f"spec §5.14 requires {field}"
    assert payload["alarms_fired"] == 1
    assert payload["detectors_run"] == ["output-drift", "silent-failure"]


def test_baselines_checked_references_nf160_pins(
    attestation: AssuranceAttestation,
) -> None:
    """The two halves of ADR-0147 must connect."""
    assert attestation.baselines_checked == ["bl-support-agent-golden-2026Q2"]


def test_a_run_that_checked_nothing_is_recorded_not_refused() -> None:
    """An empty run is a fact worth recording, not an error."""
    a = record_run("s", ran_at=RAN_AT, cadence_seconds=DAY)
    assert a.baselines_checked == []
    assert a.alarms_fired == 0


# ── next_due is derived, not claimed ─────────────────────────────────────────


def test_next_due_is_computed_from_ran_at_plus_cadence(
    attestation: AssuranceAttestation,
) -> None:
    assert attestation.next_due == "2026-07-13T00:00:00Z"


def test_next_due_cannot_be_supplied_by_the_caller() -> None:
    """A caller-chosen next_due could promise a date that never arrives."""
    with pytest.raises(TypeError):
        record_run(  # type: ignore[call-arg]
            "s", ran_at=RAN_AT, cadence_seconds=DAY, next_due="2099-01-01T00:00:00Z"
        )


def test_a_non_positive_cadence_is_refused() -> None:
    """Cadence <= 0 would make next_due meaningless and the check unmeetable."""
    for bad in (0, -1):
        with pytest.raises(AttestationError):
            record_run("s", ran_at=RAN_AT, cadence_seconds=bad)


# ── detecting the run that never happened ────────────────────────────────────


def test_a_missed_run_is_detected_from_the_previous_success(
    attestation: AssuranceAttestation,
) -> None:
    """The artifact that proves the failure is the last success."""
    verdict = check_overdue(attestation, now="2026-07-15T00:00:00Z")

    assert verdict.overdue is True
    assert verdict.late_by_seconds == 2 * DAY
    assert verdict.last_ran_at == RAN_AT


def test_an_on_time_schedule_is_not_overdue(
    attestation: AssuranceAttestation,
) -> None:
    verdict = check_overdue(attestation, now="2026-07-12T12:00:00Z")
    assert verdict.overdue is False
    assert verdict.late_by_seconds == 0


def test_exactly_due_is_not_yet_overdue(attestation: AssuranceAttestation) -> None:
    """The boundary: firing at the instant a run comes due would call every
    on-time schedule late."""
    verdict = check_overdue(attestation, now=attestation.next_due)

    assert verdict.overdue is False
    assert verdict.late_by_seconds == 0


def test_one_second_past_due_is_overdue(attestation: AssuranceAttestation) -> None:
    verdict = check_overdue(attestation, now="2026-07-13T00:00:01Z")
    assert verdict.overdue is True
    assert verdict.late_by_seconds == 1


# ── timestamps are validated, not trusted ────────────────────────────────────


@pytest.mark.parametrize("bad", ["yesterday", "2026-13-01T00:00:00Z", ""])
def test_an_unparseable_timestamp_is_refused(bad: str) -> None:
    with pytest.raises(AttestationError, match="RFC 3339"):
        record_run("s", ran_at=bad, cadence_seconds=DAY)


def test_a_naive_timestamp_is_refused() -> None:
    """Without an offset the arithmetic silently depends on the host's clock."""
    with pytest.raises(AttestationError, match="UTC offset"):
        record_run("s", ran_at="2026-07-12T00:00:00", cadence_seconds=DAY)


def test_offsets_are_normalised_to_utc() -> None:
    a = record_run("s", ran_at="2026-07-12T02:00:00+02:00", cadence_seconds=DAY)
    assert a.ran_at == "2026-07-12T00:00:00Z"
    assert a.next_due == "2026-07-13T00:00:00Z"


def test_an_empty_schedule_id_is_refused() -> None:
    with pytest.raises(AttestationError):
        record_run("  ", ran_at=RAN_AT, cadence_seconds=DAY)


# ── sealing, additivity, fail-open ───────────────────────────────────────────


def test_it_seals_through_the_existing_intoto_seam(
    attestation: AssuranceAttestation,
) -> None:
    """No third top-level format (ADR-0034)."""
    predicate = into_predicate(attestation)

    assert set(predicate) == {PREDICATE_KEY}
    assert predicate[PREDICATE_KEY]["schedule_id"] == "nightly-assurance"


def test_the_predicate_fragment_is_accepted_by_capsule_statement(
    attestation: AssuranceAttestation, tmp_path
) -> None:
    """Proves the seam actually fits, rather than assuming it does."""
    from novafabric.envelopes.intoto import capsule_statement

    capsule = tmp_path / "capsule"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("run_id: run-1\n", encoding="utf-8")

    statement = capsule_statement(capsule, extra_predicate=into_predicate(attestation))

    assert statement["predicate"][PREDICATE_KEY]["next_due"] == attestation.next_due


def test_no_attestation_leaves_the_capsule_byte_identical() -> None:
    capsule = {"run_id": "r", "facets": {"other": {"x": 1}}}
    original = json.dumps(capsule, sort_keys=True)

    assert attach_facet(capsule, None) == capsule
    assert json.dumps(capsule, sort_keys=True) == original


def test_round_trip_through_a_capsule(attestation: AssuranceAttestation) -> None:
    capsule = attach_facet({"run_id": "r"}, attestation)
    assert facet_from_capsule(capsule) == attestation


def test_a_capsule_without_one_reads_as_none() -> None:
    assert facet_from_capsule({"run_id": "r"}) is None
    assert facet_from_capsule({"facets": {}}) is None


def test_an_invalid_attestation_is_reported() -> None:
    with pytest.raises(AttestationError, match="invalid assurance attestation"):
        facet_from_capsule({"facets": {"assurance_attestation": {"schedule_id": "s"}}})
