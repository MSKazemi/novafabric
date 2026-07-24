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

"""ADR-0162 P1 — sensor provenance + actuation records (NF-301/NF-302).

Tests are organised by the ADR's invariants, because those are what a
reviewer needs to be convinced of: I-1 additive-first, I-2 no raw payloads,
I-3 record-only/fail-open, I-4 declared-is-not-observed. The three golden
fixtures ADR-0162 P1 names live at the end.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from novafabric.embodied import (
    FACET_NAME,
    ActuationRecord,
    EmbodiedFacet,
    InvalidReferenceError,
    MissingIssuerError,
    RawPayloadRejectedError,
    SensorStream,
    attach_facet,
    build_actuation,
    build_facet,
    digest_stream,
    is_confirmed,
    reject_raw_payloads,
    verify_receipt_binding,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "run-capsule.schema.json"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "embodied"
TEXT_ONLY_CAPSULE = (
    REPO_ROOT / "tests" / "fixtures" / "model-provenance" / "valid-text-only-capsule.json"
)

RECEIPT = b"action-receipt: motion x941 confirmed by control plane"
REF_A = f"sha256:{'a' * 64}"
REF_B = f"sha256:{'b' * 64}"

#: A stand-in for a decoded camera frame. Long enough and pure enough to trip
#: the inline-payload bound; deliberately not valid base64 *content*, because
#: the module must key on shape, not on being able to decode it.
FRAME_B64 = "QUJDRA" * 60


def _stream(**kw: Any) -> SensorStream:
    base: dict[str, Any] = {
        "sensor_id": "front-cam-0",
        "modality": "camera",
        "frame_count": 18240,
        "stream_digest": REF_A,
        "clock_domain": "ptp-0",
    }
    base.update(kw)
    return SensorStream(**base)


def _command(**kw: Any) -> ActuationRecord:
    base: dict[str, Any] = {
        "command_class": "motion",
        "target_ref": REF_A,
        "count": 941,
        "issued_by": "run:01HXAY",
    }
    base.update(kw)
    return ActuationRecord(**base)


# ── Digest binding ────────────────────────────────────────────────────────


def test_digest_is_sha256_of_content_with_algorithm_prefix() -> None:
    expected = hashlib.sha256(RECEIPT).hexdigest()
    assert digest_stream(RECEIPT) == f"sha256:{expected}"


def test_digest_accepts_bytes_and_str_identically() -> None:
    assert digest_stream("abc") == digest_stream(b"abc")


def test_receipt_binding_verifies_against_the_receipt() -> None:
    assert verify_receipt_binding(digest_stream(RECEIPT), RECEIPT) is True
    assert verify_receipt_binding(digest_stream(RECEIPT), RECEIPT + b" ") is False


def test_missing_receipt_ref_does_not_verify() -> None:
    """The case the verifier exists to surface, not a trivial pass."""
    assert verify_receipt_binding(None, RECEIPT) is False


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-digest",
        "md5:abc",
        "sha256:zz",
        "https://fleet.example/streams/front-cam-0",  # a URI names a place, not bytes
    ],
)
def test_non_digest_reference_is_refused_by_name(bad: str) -> None:
    with pytest.raises(InvalidReferenceError):
        _stream(stream_digest=bad)


def test_reference_errors_are_not_swallowed_into_a_validation_error() -> None:
    """Pydantic v2 folds a validator's ValueError into ValidationError.

    If these exceptions ever subclass ValueError, the named type is destroyed
    and a caller who inlined a frame gets a generic shape complaint instead of
    being told what they actually did.
    """
    assert not issubclass(InvalidReferenceError, ValueError)
    assert not issubclass(RawPayloadRejectedError, ValueError)
    assert not issubclass(MissingIssuerError, ValueError)


# ── I-2: no raw payloads ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("payload", "label"),
    [
        (b"\x89PNG\r\n\x1a\n", "bytes"),
        (bytearray(b"\x89PNG"), "bytearray"),
        (memoryview(b"\x89PNG"), "memoryview"),
    ],
)
def test_binary_buffers_are_refused_wherever_a_digest_belongs(
    payload: Any, label: str
) -> None:
    """A caller who passed the frame itself must be told so, by name."""
    with pytest.raises(RawPayloadRejectedError):
        _stream(stream_digest=payload)


def test_long_base64_string_is_refused_as_an_inlined_payload() -> None:
    with pytest.raises(RawPayloadRejectedError):
        _stream(extra_capture=FRAME_B64)


def test_base64_data_uri_is_refused_at_any_length() -> None:
    """Unambiguous: a base64 data URI *is* an inlined payload."""
    with pytest.raises(RawPayloadRejectedError):
        _stream(thumbnail="data:image/png;base64,iVBORw0KGgo=")


def test_short_opaque_identifiers_are_not_mistaken_for_payloads() -> None:
    """The bound must not reject the refs the facet exists to carry.

    A sha256 ref is 71 pure-base64-alphabet characters. If the bound ever
    drops below that, every legitimate stream_digest becomes a payload.
    """
    stream = _stream(vendor_stream_id="AbCdEf0123456789", c2pa_manifest_ref=REF_B)
    assert stream.c2pa_manifest_ref == REF_B


def test_long_prose_is_left_alone() -> None:
    """One-sided on purpose: rejecting long non-base64 text would be this
    module policing content rather than enforcing reference-not-bytes."""
    stream = _stream(note="fog rolled in over the yard " * 40)
    assert stream.sensor_id == "front-cam-0"


class _FakeNdarray:
    """A numpy-ish object, duck-typed the way the module detects one.

    Constructed here rather than importing numpy: the module must detect an
    array without taking a runtime dependency on the array libraries
    (ADR-0024), so the test must not smuggle one in either.
    """

    shape = (480, 640, 3)
    dtype = "uint8"

    def tobytes(self) -> bytes:
        return b"\x00" * 8


def test_array_like_object_is_refused() -> None:
    with pytest.raises(RawPayloadRejectedError):
        _stream(frame_buffer=_FakeNdarray())


def test_array_interface_object_is_refused() -> None:
    class _Exporter:
        __array_interface__ = {"shape": (2,), "typestr": "|u1"}

    with pytest.raises(RawPayloadRejectedError):
        _stream(depth_map=_Exporter())


@pytest.mark.parametrize(
    "key",
    ["frames", "image", "point_cloud", "pointcloud", "video", "audio", "pixels", "raw"],
)
def test_payload_named_fields_are_refused_even_when_empty(key: str) -> None:
    """A field *named* for a payload is refused on its name alone.

    An empty `image` today is an inlined frame after the caller's next commit,
    and the sealed root is the wrong place to discover that.
    """
    with pytest.raises(RawPayloadRejectedError):
        _stream(**{key: ""})


def test_count_fields_survive_the_payload_name_rule() -> None:
    """`frame_count` contains 'frame' and must not be collateral damage."""
    assert _stream(frame_count=0).frame_count == 0


def test_payload_rejection_names_the_field_but_never_the_value() -> None:
    """The message travels into logs; the payload must not travel with it."""
    with pytest.raises(RawPayloadRejectedError) as excinfo:
        reject_raw_payloads({"sensors": [{"capture": FRAME_B64}]})
    message = str(excinfo.value)
    assert "sensors[0].capture" in message
    assert FRAME_B64 not in message


def test_payload_rejection_covers_extras_on_the_facet_itself() -> None:
    """A payload can arrive where no sub-model would ever see it."""
    with pytest.raises(RawPayloadRejectedError):
        EmbodiedFacet(sensors=[_stream()], lidar_dump=b"\x00\x01")


def test_actuation_refuses_drive_by_wire_bytes() -> None:
    with pytest.raises(RawPayloadRejectedError):
        _command(can_payload=b"\x02\x1f\x00")


# ── I-4: declared is not observed ─────────────────────────────────────────


def test_command_without_a_receipt_is_unbound() -> None:
    """A declared command NovaFabric holds no receipt for."""
    record = build_actuation(
        command_class="motion", target_ref=REF_A, count=941, issued_by="run:1"
    )
    assert record.unbound is True
    assert is_confirmed(record) is False


def test_receipt_that_resolves_makes_a_command_confirmed() -> None:
    ref = digest_stream(RECEIPT)
    record = build_actuation(
        command_class="motion",
        target_ref=REF_A,
        count=941,
        issued_by="run:1",
        action_receipt_ref=ref,
        resolver=lambda _: RECEIPT,
    )
    assert record.unbound is False
    assert is_confirmed(record) is True


def test_unresolvable_receipt_is_unbound_not_confirmed() -> None:
    record = build_actuation(
        command_class="motion",
        target_ref=REF_A,
        count=941,
        issued_by="run:1",
        action_receipt_ref=digest_stream(RECEIPT),
        resolver=lambda _: None,
    )
    assert record.unbound is True
    assert is_confirmed(record) is False


def test_receipt_naming_different_bytes_is_unbound_not_confirmed() -> None:
    """The more dangerous of the two failures.

    A lookup that succeeded but returned something else must not read as
    confirmation merely because the resolver did not return None — that would
    record a commanded motion as one a receipt attests to.
    """
    record = build_actuation(
        command_class="motion",
        target_ref=REF_A,
        count=941,
        issued_by="run:1",
        action_receipt_ref=digest_stream(RECEIPT),
        resolver=lambda _: b"some other receipt entirely",
    )
    assert record.unbound is True
    assert is_confirmed(record) is False


def test_unchecked_receipt_is_not_reported_as_a_finding() -> None:
    """No resolver means no check was possible, not that the ref failed."""
    record = build_actuation(
        command_class="motion",
        target_ref=REF_A,
        count=941,
        issued_by="run:1",
        action_receipt_ref=REF_B,
    )
    assert record.unbound is False


def test_receiptless_command_cannot_be_marked_bound_on_any_path() -> None:
    """The single most dangerous field combination this module could permit.

    `unbound: false` with no receipt would read, to anyone reconstructing a
    physical incident, as a *confirmed* motion. Direct construction and
    `model_validate` of untrusted JSON must both be unable to produce it.
    """
    assert _command(unbound=False).unbound is True
    parsed = ActuationRecord.model_validate(
        {
            "command_class": "grip",
            "target_ref": REF_A,
            "count": 1,
            "issued_by": "run:1",
            "unbound": False,
        }
    )
    assert parsed.unbound is True
    assert is_confirmed(parsed) is False


def test_command_must_name_its_issuer() -> None:
    """Without it, a declared command is indistinguishable from one
    NovaFabric issued — and NovaFabric issues nothing."""
    with pytest.raises(MissingIssuerError):
        _command(issued_by="   ")


def test_a_resolver_that_raises_does_not_fail_the_capsule() -> None:
    """Fail-open (I-3): a lookup error is a fact about the resolver.

    Failing here would let an evidence lookup take down a robot's capsule.
    """

    def _boom(_: str) -> bytes:
        raise RuntimeError("receipt store unreachable")

    record = build_actuation(
        command_class="motion",
        target_ref=REF_A,
        count=941,
        issued_by="run:1",
        action_receipt_ref=REF_B,
        resolver=_boom,
    )
    assert record.unbound is True


# ── I-1: additive-first / absent is not false ─────────────────────────────


def test_no_embodied_material_writes_no_facet() -> None:
    """Absent means *not recorded* — never "the body did nothing"."""
    assert build_facet() is None


def test_capsule_without_embodied_material_is_untouched() -> None:
    capsule = {"run_id": "r", "facets": {"existing": {"a": 1}}}
    assert attach_facet(capsule, build_facet()) == capsule


def test_attach_does_not_mutate_the_input_capsule() -> None:
    capsule: dict[str, Any] = {"run_id": "r"}
    attach_facet(capsule, build_facet(sensors=[_stream()]))
    assert capsule == {"run_id": "r"}


def test_attach_preserves_sibling_facets() -> None:
    capsule = {"run_id": "r", "facets": {"existing": {"a": 1}}}
    out = attach_facet(capsule, build_facet(sensors=[_stream()]))
    assert out["facets"]["existing"] == {"a": 1}
    assert FACET_NAME in out["facets"]


def test_recorded_zero_is_distinct_from_an_absent_record() -> None:
    """A stream counted at zero frames is evidence; a missing stream is not."""
    facet = build_facet(sensors=[_stream(frame_count=0)])
    assert facet is not None
    assert facet.sensors[0].frame_count == 0
    assert build_facet(sensors=[]) is None


def test_facet_ordering_is_stable_across_captures() -> None:
    late, early = _stream(sensor_id="lidar-top"), _stream(sensor_id="front-cam-0")
    assert [s.sensor_id for s in build_facet(sensors=[late, early]).sensors] == [  # type: ignore[union-attr]
        "front-cam-0",
        "lidar-top",
    ]


def test_facet_carries_a_schema_version() -> None:
    out = attach_facet({"run_id": "r"}, build_facet(sensors=[_stream()]))
    assert out["facets"][FACET_NAME]["schema_version"]


def test_unbound_survives_exclude_none_in_the_sealed_record() -> None:
    """A command's *lack* of confirmation cannot be optimised out."""
    out = attach_facet({"run_id": "r"}, build_facet(actuation=[_command()]))
    assert out["facets"][FACET_NAME]["actuation"][0]["unbound"] is True


def test_later_phase_objects_are_absent_rather_than_stubbed() -> None:
    """An empty `odd` block would read as "checked, nothing found"."""
    dumped = EmbodiedFacet(sensors=[_stream()]).model_dump(exclude_none=True)
    assert {"odd", "sim2real", "teleop", "timing", "trajectory"}.isdisjoint(dumped)


# ── I-3: record-only ──────────────────────────────────────────────────────


def test_module_exposes_no_actuation_surface() -> None:
    """Record-only is a property of the API, not just of the docs.

    NovaFabric must never be able to issue, gate, or block a physical command.
    If such an entry point ever appears here, this fails — which is the point.
    """
    import novafabric.embodied as embodied

    forbidden = {
        "actuate",
        "issue",
        "send",
        "execute",
        "command",
        "drive",
        "fly",
        "move",
        "gate",
        "block",
        "enforce",
        "stop",
        "control",
        "plan",
    }
    exported_tokens = {
        token.lower() for name in embodied.__all__ for token in name.split("_")
    }
    assert forbidden.isdisjoint(exported_tokens), (
        "facets.embodied must not expose an actuation or gating entry point "
        "(ADR-0162 I-3 — NovaFabric is never in a control hot path)"
    )


def test_no_verdict_surface_is_exported() -> None:
    """Safety adjudication is the operator's assurance case, not ours."""
    import novafabric.embodied as embodied

    assert not [n for n in embodied.__all__ if "verdict" in n.lower()]


# ── Golden fixtures (the three ADR-0162 P1 names) ─────────────────────────


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


def test_golden_text_only_capsule_stays_valid(schema: dict[str, Any]) -> None:
    """Fixture 1: a capsule from before this feature is untouched and valid."""
    capsule = json.loads(TEXT_ONLY_CAPSULE.read_text())
    assert "facets" not in capsule
    jsonschema.validate(capsule, schema)
    assert attach_facet(capsule, build_facet()) == capsule


def test_golden_embodied_facet_round_trips(schema: dict[str, Any]) -> None:
    """Fixture 2: a valid embodied facet parses and re-serialises unchanged."""
    raw = json.loads((FIXTURES / "valid-facet.json").read_text())
    facet = EmbodiedFacet.model_validate(raw)

    assert [s.sensor_id for s in facet.sensors] == ["front-cam-0", "lidar-top"]
    assert facet.sensors[0].c2pa_manifest_ref is not None
    assert facet.verified.no_raw_payload is True

    motion = next(a for a in facet.actuation if a.command_class == "motion")
    release = next(a for a in facet.actuation if a.command_class == "payload_release")
    assert is_confirmed(motion) is True
    assert is_confirmed(release) is False

    assert facet.model_dump(exclude_none=True) == raw


def test_golden_embodied_facet_validates_against_the_real_schema(
    schema: dict[str, Any],
) -> None:
    """A facet-bearing capsule, checked against the shipped run-capsule schema.

    The gap ADR-0196 closed: five earlier facet slices passed their own gates
    on plain dicts while writing a `facets` key the real schema rejected.
    """
    capsule = json.loads(TEXT_ONLY_CAPSULE.read_text())
    facet = build_facet(sensors=[_stream()], actuation=[_command()])
    jsonschema.validate(attach_facet(capsule, facet), schema)


def test_golden_raw_payload_facet_is_rejected() -> None:
    """Fixture 3: the frame a caller inlined instead of hashing.

    Refused with a named exception rather than silently digested or truncated
    — a caller who put a camera frame in the sealed root needs to know they
    did, not to discover it in a privacy review.
    """
    raw = json.loads((FIXTURES / "raw-payload-facet.json").read_text())
    with pytest.raises(RawPayloadRejectedError) as excinfo:
        EmbodiedFacet.model_validate(raw)
    assert "frames" in str(excinfo.value)
