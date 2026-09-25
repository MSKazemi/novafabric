"""ADR-0162 P2 — NF-303 ODD conformance as a record, never a verdict.

Acceptance criteria (spec §6 "ODD record, no verdict"):

- a declared ODD + an observed fog excursion records ``in_odd: false`` and
  ``verdict: null``, and ``verdict: null`` survives ``exclude_none``;
- a non-null ``verdict`` or an ``in_odd: true`` excursion is refused by name
  on every construction path (no ruling is representable);
- excursions are time-ordered; out-of-order input is refused, ``build_odd``
  sorts stably;
- zero excursions is recordable (declared ODD, nothing recorded) and an
  absent ODD writes no key (absent is not false);
- the I-2 reference-not-bytes boundary applies to the ODD block too.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError

from novafabric.embodied import (
    FACET_NAME,
    AdjudicationRefusedError,
    EmbodiedFacet,
    ExcursionOrderError,
    InvalidReferenceError,
    InvalidTimestampError,
    OddConformance,
    OddExcursion,
    RawPayloadRejectedError,
    attach_facet,
    build_facet,
    build_odd,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "embodied"
SCHEMA_PATH = REPO_ROOT / "schemas" / "run-capsule.schema.json"
TEXT_ONLY_CAPSULE = (
    REPO_ROOT / "tests" / "fixtures" / "model-provenance" / "valid-text-only-capsule.json"
)
ODD_REF = f"sha256:{'c' * 64}"


def _excursion(**kw: Any) -> OddExcursion:
    base: dict[str, Any] = {
        "condition": "fog",
        "observed": "visibility<40m",
        "ts": "2026-07-13T10:02:11Z",
    }
    base.update(kw)
    return OddExcursion(**base)


# ── Record, never a ruling (D2 / I-4) ─────────────────────────────────────


def test_fog_excursion_is_recorded_out_of_odd_with_a_null_verdict() -> None:
    odd = build_odd(odd_ref=ODD_REF, excursions=[_excursion()])
    assert odd.verdict is None
    assert odd.excursions[0].in_odd is False
    dumped = odd.model_dump(exclude_none=True)
    assert dumped["verdict"] is None
    assert dumped["excursions"][0]["in_odd"] is False


def test_null_verdict_survives_attach_into_the_capsule() -> None:
    out = attach_facet({"run_id": "r"}, build_facet(odd=build_odd(odd_ref=ODD_REF)))
    block = out["facets"][FACET_NAME]
    assert "verdict" in block["odd"] and block["odd"]["verdict"] is None
    assert block["verified"]["odd_verdict_is_null"] is True


@pytest.mark.parametrize("ruling", ["safe", "unsafe", True, False, 0, {"in_odd": True}])
def test_any_non_null_verdict_is_refused_by_name(ruling: Any) -> None:
    with pytest.raises(AdjudicationRefusedError) as excinfo:
        OddConformance.model_validate({"odd_ref": ODD_REF, "verdict": ruling})
    assert excinfo.value.field == "verdict"
    assert "never a ruling" in str(excinfo.value)


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_verdict_cannot_be_set_through_model_copy_either() -> None:
    """``model_copy(update=…)`` skips validation — the dump must still say null."""
    odd = build_odd(odd_ref=ODD_REF).model_copy(update={"verdict": "safe"})
    assert odd.model_dump()["verdict"] is None


@pytest.mark.parametrize("claim", [True, 1, "true", "yes"])
def test_an_in_odd_excursion_is_a_ruling_and_is_refused(claim: Any) -> None:
    with pytest.raises(AdjudicationRefusedError) as excinfo:
        _excursion(in_odd=claim)
    assert excinfo.value.field == "in_odd"


def test_adjudication_error_is_not_folded_into_a_validation_error() -> None:
    assert not issubclass(AdjudicationRefusedError, ValueError)


def test_derived_verified_flag_ignores_caller_input() -> None:
    no_odd = EmbodiedFacet.model_validate(
        {"verified": {"no_raw_payload": True, "odd_verdict_is_null": False}}
    )
    assert no_odd.verified.odd_verdict_is_null is None
    with_odd = EmbodiedFacet.model_validate(
        {"odd": {"odd_ref": ODD_REF}, "verified": {"odd_verdict_is_null": False}}
    )
    assert with_odd.verified.odd_verdict_is_null is True


# ── Order is evidence ────────────────────────────────────────────────────


def test_build_odd_sorts_excursions_by_instant_stably() -> None:
    late = _excursion(condition="speed", ts="2026-07-13T10:05:00Z")
    early = _excursion(condition="fog", ts="2026-07-13T10:02:00Z")
    tie = _excursion(condition="night", ts="2026-07-13T10:02:00+00:00")
    odd = build_odd(odd_ref=ODD_REF, excursions=[late, early, tie])
    assert [e.condition for e in odd.excursions] == ["fog", "night", "speed"]


def test_offsets_are_compared_as_instants_not_strings() -> None:
    # 11:00+02:00 is 09:00Z — earlier than 10:00Z despite sorting later as text.
    odd = build_odd(
        odd_ref=ODD_REF,
        excursions=[
            _excursion(ts="2026-07-13T10:00:00Z"),
            _excursion(ts="2026-07-13T11:00:00+02:00"),
        ],
    )
    assert odd.excursions[0].ts == "2026-07-13T11:00:00+02:00"


def test_out_of_order_excursions_are_refused_on_load() -> None:
    with pytest.raises(ExcursionOrderError, match=r"excursions\[1\]"):
        OddConformance.model_validate(
            {
                "odd_ref": ODD_REF,
                "excursions": [
                    {"condition": "a", "observed": "x", "ts": "2026-07-13T10:05:00Z"},
                    {"condition": "b", "observed": "y", "ts": "2026-07-13T10:00:00Z"},
                ],
            }
        )


@pytest.mark.parametrize("ts", ["2026-07-13T10:00:00", "yesterday", "", 1720864800])
def test_naive_or_unparseable_timestamps_are_refused(ts: Any) -> None:
    with pytest.raises(InvalidTimestampError):
        _excursion(ts=ts)


def test_timestamp_is_stored_exactly_as_declared() -> None:
    assert _excursion(ts="2026-07-13T10:02:11.500+01:00").ts == "2026-07-13T10:02:11.500+01:00"


# ── Absent is not false ──────────────────────────────────────────────────


def test_declared_odd_with_no_excursions_is_recordable() -> None:
    odd = build_odd(odd_ref=ODD_REF)
    assert odd.excursions == []
    assert build_facet(odd=odd) is not None


def test_no_odd_writes_no_odd_key() -> None:
    dumped = attach_facet({}, build_facet(trajectory=[]))
    assert dumped == {}


def test_empty_condition_is_refused() -> None:
    with pytest.raises(ValidationError):
        _excursion(condition="")


# ── I-2 boundary + references ────────────────────────────────────────────


def test_odd_ref_must_be_a_digest() -> None:
    with pytest.raises(InvalidReferenceError):
        build_odd(odd_ref="https://example.invalid/odd.yaml")


def test_odd_ref_offered_as_bytes_is_a_raw_payload() -> None:
    with pytest.raises(RawPayloadRejectedError):
        OddConformance.model_validate({"odd_ref": b"\x00" * 16})


def test_camera_frame_smuggled_into_an_excursion_is_refused() -> None:
    with pytest.raises(RawPayloadRejectedError, match="image"):
        _excursion(image="")


def test_payload_as_extra_on_the_odd_block_is_refused() -> None:
    with pytest.raises(RawPayloadRejectedError):
        OddConformance.model_validate({"odd_ref": ODD_REF, "frames": []})


# ── Golden fixtures ──────────────────────────────────────────────────────


def test_golden_odd_facet_round_trips_and_validates_against_the_schema() -> None:
    raw = json.loads((FIXTURES / "valid-odd-trajectory-facet.json").read_text())
    facet = EmbodiedFacet.model_validate(raw)
    assert facet.odd is not None and facet.odd.verdict is None
    assert [e.condition for e in facet.odd.excursions] == ["fog", "speed"]
    assert facet.model_dump(exclude_none=True) == raw

    capsule = json.loads(TEXT_ONLY_CAPSULE.read_text())
    capsule["facets"] = {FACET_NAME: raw}
    jsonschema.validate(capsule, json.loads(SCHEMA_PATH.read_text()))


def test_golden_non_null_verdict_fixture_is_refused() -> None:
    raw = json.loads((FIXTURES / "odd-nonnull-verdict-facet.json").read_text())
    with pytest.raises(AdjudicationRefusedError):
        EmbodiedFacet.model_validate(raw)


def test_non_string_odd_ref_is_a_shape_error() -> None:
    with pytest.raises(ValidationError):
        OddConformance.model_validate({"odd_ref": 12345})
