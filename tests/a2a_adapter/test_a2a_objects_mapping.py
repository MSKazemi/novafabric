"""A2A object mapping and round-trip — ADR-0149 D1 / NF-172.

The test that matters most is `test_the_roundtrip_is_not_vacuous`. A digest taken
over the facet's own storage would be re-derived from the same bytes on re-export
and match unconditionally — a round-trip check that cannot fail. So these tests
tamper with each object kind and require the check to fail *and name what changed*.
"""

from __future__ import annotations

import json

import pytest

from novafabric.a2a.objects import (
    A2AObjectsError,
    attach_facet,
    export_objects,
    facet_from_capsule,
    map_objects,
    parts_digest,
    roundtrip,
)

TASK = {"id": "task-1", "state": "completed", "sessionId": "s-9"}
MESSAGE = {
    "messageId": "msg-1",
    "role": "agent",
    "parts": [{"kind": "text", "text": "the secret plan"}],
    "metadata": {"lang": "en"},
}
ARTIFACT = {"name": "report.pdf", "content_hash": "sha256:" + "c" * 64,
            "description": "final report"}


@pytest.fixture()
def facet():
    f = map_objects(tasks=[TASK], messages=[MESSAGE], artifacts=[ARTIFACT])
    assert f is not None
    return f


# ── the MUST fields ──────────────────────────────────────────────────────────


def test_all_three_object_kinds_are_mapped(facet) -> None:
    assert facet.tasks[0].task_id == "task-1"
    assert facet.tasks[0].lifecycle_state == "completed"
    assert facet.messages[0].message_id == "msg-1"
    assert facet.messages[0].role == "agent"
    assert facet.artifacts[0].artifact_name == "report.pdf"
    assert facet.artifacts[0].content_hash == ARTIFACT["content_hash"]


def test_the_manifest_records_field_by_field_correspondence(facet) -> None:
    rows = {(e.kind, e.a2a_field, e.capsule_field) for e in facet.mapping_manifest}
    assert ("task", "id", "task_id") in rows
    assert ("message", "parts", "parts_digest") in rows
    assert ("artifact", "content_hash", "content_hash") in rows


def test_message_parts_are_bound_by_digest_never_stored(facet) -> None:
    """Parts carry user content (ADR-0009); the capsule must not hold them."""
    serialised = json.dumps(facet.model_dump())

    assert "the secret plan" not in serialised
    assert facet.messages[0].parts_digest == parts_digest(MESSAGE["parts"])


# ── nothing is dropped silently ──────────────────────────────────────────────


def test_unmapped_fields_are_enumerated_per_object(facet) -> None:
    assert facet.tasks[0].unmapped == ["sessionId"]
    assert facet.messages[0].unmapped == ["metadata"]
    assert facet.artifacts[0].unmapped == ["description"]


def test_a_fully_mapped_object_has_nothing_unmapped() -> None:
    f = map_objects(tasks=[{"id": "t", "state": "working"}])
    assert f is not None
    assert f.tasks[0].unmapped == []


# ── the round-trip, and its non-vacuity ──────────────────────────────────────


def test_the_roundtrip_matches_on_untouched_objects(facet) -> None:
    result = roundtrip(facet)
    assert result.matches is True
    assert result.observed_digest == facet.roundtrip_digest
    assert result.unmapped_total == 3


@pytest.mark.parametrize(
    ("collection", "field", "new", "expected_kind"),
    [
        ("tasks", "lifecycle_state", "failed", "task"),
        ("tasks", "task_id", "task-999", "task"),
        ("messages", "role", "user", "message"),
        ("messages", "parts_digest", "sha256:" + "0" * 64, "message"),
        ("artifacts", "artifact_name", "other.pdf", "artifact"),
        ("artifacts", "content_hash", "sha256:" + "9" * 64, "artifact"),
    ],
)
def test_the_roundtrip_is_not_vacuous(
    facet, collection: str, field: str, new: str, expected_kind: str
) -> None:
    """Tamper with one mapped field; the round-trip must fail AND name the object.

    If `roundtrip_digest` were taken over the facet's own storage, every one of
    these would still pass — which is exactly why it is taken over the re-export.
    """
    items = list(getattr(facet, collection))
    items[0] = items[0].model_copy(update={field: new})
    tampered = facet.model_copy(update={collection: items})

    result = roundtrip(tampered)

    assert result.matches is False, f"tampering with {field} went undetected"
    assert result.diverging, "a mismatch must name what diverged"
    assert result.diverging[0]["kind"] == expected_kind
    # The diverging entry must carry enough to locate the object, not just say
    # "something changed".
    assert result.diverging[0]["recorded_object_digest"] != (
        result.diverging[0]["observed_object_digest"]
    )
    assert result.diverging[0]["fields"], "must name the fields of the changed object"


def test_a_mismatch_never_rewrites_the_recorded_digest(facet) -> None:
    items = list(facet.tasks)
    items[0] = items[0].model_copy(update={"lifecycle_state": "failed"})
    tampered = facet.model_copy(update={"tasks": items})
    before = tampered.model_dump()

    roundtrip(tampered)

    assert tampered.model_dump() == before


def test_the_export_is_a2a_shaped(facet) -> None:
    exported = export_objects(facet)
    kinds = [o["kind"] for o in exported]

    assert kinds == ["task", "message", "artifact"]
    assert exported[0]["id"] == "task-1"
    assert exported[1]["messageId"] == "msg-1"
    assert "parts" not in exported[1], "parts were never stored, so cannot be re-emitted"
    assert exported[1]["parts_digest"] == facet.messages[0].parts_digest


# ── validation and fail-open ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kwargs", "missing"),
    [
        ({"tasks": [{"state": "done"}]}, "id"),
        ({"tasks": [{"id": "t"}]}, "state"),
        ({"messages": [{"role": "agent", "parts": []}]}, "messageId"),
        ({"artifacts": [{"name": "a"}]}, "content_hash"),
    ],
)
def test_a_missing_required_field_is_refused(kwargs, missing: str) -> None:
    with pytest.raises(A2AObjectsError, match=missing):
        map_objects(**kwargs)


def test_message_parts_must_be_a_list() -> None:
    with pytest.raises(A2AObjectsError, match="parts"):
        map_objects(messages=[{"messageId": "m", "role": "agent", "parts": "nope"}])


def test_no_objects_means_no_facet() -> None:
    assert map_objects() is None


def test_no_facet_leaves_the_capsule_byte_identical() -> None:
    capsule = {"run_id": "r", "facets": {"other": {"x": 1}}}
    original = json.dumps(capsule, sort_keys=True)

    assert attach_facet(capsule, None) == capsule
    assert json.dumps(capsule, sort_keys=True) == original


def test_round_trip_through_a_capsule(facet) -> None:
    capsule = attach_facet({"run_id": "r"}, facet)
    assert facet_from_capsule(capsule) == facet
    assert roundtrip(facet_from_capsule(capsule)).matches is True


def test_an_invalid_facet_is_reported(facet) -> None:
    with pytest.raises(A2AObjectsError, match="invalid a2a_objects facet"):
        facet_from_capsule({"facets": {"a2a_objects": {"tasks": "not-a-list"}}})
