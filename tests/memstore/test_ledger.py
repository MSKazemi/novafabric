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

"""ADR-0171 P1 — KB-mutation ledger + facet (NF-391).

Tests are organised by the ADR's invariants, because those are what a reviewer
needs to be convinced of: I-1 record-only/store-external, I-2 digests-only,
I-3 fail-open/additive, I-4 append-only and tamper-evident.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from novafabric.memstore import (
    ContentCaptureError,
    InvalidDigestError,
    LedgerRewriteError,
    MemstoreError,
    MemstoreMutationFacet,
    MutationActor,
    MutationRecord,
    append_mutation,
    attach_facet,
    build_facet,
    chain_head,
    digest_value,
    facet_for_ledger,
    facet_from_capsule,
    record_digest,
    record_preimage,
    scan_for_content,
    verify_append_only,
    verify_chain,
    verify_facet_binding,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "run-capsule.schema.json"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "memstore"

#: The two golden fixtures ADR-0171 P1 names.
STORE_LESS_CAPSULE = FIXTURES / "store-less-capsule.json"
VALID_LEDGER = FIXTURES / "valid-mutation-ledger.json"

STORE = "org-kb/support-playbooks"
CONTENT_V1 = "playbook: reset the customer's session, then escalate to tier 2"
CONTENT_V2 = "playbook: verify identity, reset the session, escalate to tier 2"


def _actor(agent: str = "triage-agent", run: str | None = "run_8f3bd21e") -> MutationActor:
    return MutationActor(agent=agent, run=run)


def _ledger(count: int = 3) -> list[MutationRecord]:
    """Build a well-formed chain of *count* records."""
    records: list[MutationRecord] = []
    for index in range(count):
        records = append_mutation(
            records,
            store_id=STORE,
            namespace="playbooks",
            entry_id=f"pb-{4471 + index}",
            op="create" if index == 0 else "update",
            value_digest=digest_value(f"value-{index}"),
            prev_value_digest=None if index == 0 else digest_value(f"value-{index - 1}"),
            by=_actor(),
            at=f"2026-07-13T09:0{index}:00Z",
        )
    return records


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture
def capsule() -> dict[str, Any]:
    return json.loads(STORE_LESS_CAPSULE.read_text())


# ── Hash construction: a chain, not a tree ────────────────────────────────


def test_record_digest_is_raw_sha256_over_the_documented_preimage() -> None:
    """The construction guard.

    Asserted against `hashlib.sha256` directly, not against either Merkle
    module. The repo carries two incompatible Merkle constructions
    (`evidence/merkle.py` RFC 6962 vs `trust/novaseal/merkle.py` pairwise+pad),
    and mixing one's leaves with the other's combiner yields a wrong root that
    still looks like a root. This module computes no tree at all; if a leaf
    prefix, a domain separator, or a tree ever creeps in here, this fails.
    """
    record = _ledger(1)[0]
    preimage = record_preimage(record)
    expected = hashlib.sha256(preimage.encode()).hexdigest()
    assert record_digest(record) == f"sha256:{expected}"


def test_preimage_is_canonical_json_sorted_and_compact() -> None:
    """The interoperability contract a non-Python verifier must reproduce."""
    record = _ledger(1)[0]
    preimage = record_preimage(record)
    payload = json.loads(preimage)
    assert preimage == json.dumps(payload, separators=(",", ":"), sort_keys=True)


def test_preimage_omits_absent_optional_fields_rather_than_nulling_them() -> None:
    """A record with no `run` must not hash as one with `run: null`.

    Otherwise the chain digest would depend on which optional fields a given
    NovaFabric version happened to know about, and a ledger written by two
    versions of the same tool would fail to verify against itself.
    """
    record = append_mutation(
        [],
        store_id=STORE,
        namespace="playbooks",
        entry_id="pb-1",
        op="create",
        value_digest=digest_value(CONTENT_V1),
        by=MutationActor(agent="compactor"),
        at="2026-07-13T09:00:00Z",
    )[0]
    payload = json.loads(record_preimage(record))
    assert "run" not in payload["by"]
    assert "prev_value_digest" not in payload
    assert "prev_record_hash" not in payload


def test_value_digest_is_plain_sha256_of_the_value() -> None:
    assert digest_value(CONTENT_V1) == f"sha256:{hashlib.sha256(CONTENT_V1.encode()).hexdigest()}"


def test_value_digest_accepts_str_and_bytes_identically() -> None:
    assert digest_value(CONTENT_V1) == digest_value(CONTENT_V1.encode())


def test_record_digest_changes_when_any_field_changes() -> None:
    record = _ledger(1)[0]
    other = record.model_copy(update={"at": "2026-07-13T09:00:01Z"})
    assert record_digest(record) != record_digest(other)


# ── I-4: append-only and tamper-evident ───────────────────────────────────


def test_empty_ledger_verifies() -> None:
    """Absent history is "not recorded", never "tampered" (I-3)."""
    result = verify_chain([])
    assert result.ok is True
    assert result.broken_at is None


def test_single_entry_ledger_verifies() -> None:
    result = verify_chain(_ledger(1))
    assert result.ok is True


def test_genesis_record_has_no_predecessor() -> None:
    """Explicit: the first link is None, not a sentinel digest."""
    assert _ledger(1)[0].prev_record_hash is None


def test_each_record_links_to_its_predecessors_digest() -> None:
    records = _ledger(4)
    for index in range(1, len(records)):
        assert records[index].prev_record_hash == record_digest(records[index - 1])


def test_well_formed_chain_verifies() -> None:
    assert verify_chain(_ledger(5)).ok is True


def test_tampering_with_a_payload_is_detected_at_the_next_index() -> None:
    """Mutating an entry's content breaks its digest, so its successor's
    stored `prev_record_hash` no longer matches."""
    records = _ledger(4)
    records[1] = records[1].model_copy(update={"entry_id": "pb-9999"})
    result = verify_chain(records)
    assert result.ok is False
    assert result.broken_at == 2
    assert result.reason is not None and "mismatch" in result.reason


def test_tampering_with_a_records_own_link_is_detected_at_that_index() -> None:
    records = _ledger(4)
    forged = f"sha256:{'0' * 64}"
    records[2] = records[2].model_copy(update={"prev_record_hash": forged})
    result = verify_chain(records)
    assert result.ok is False
    assert result.broken_at == 2


def test_reordering_two_records_is_detected() -> None:
    records = _ledger(4)
    records[1], records[2] = records[2], records[1]
    result = verify_chain(records)
    assert result.ok is False
    # Index 1 now holds a record whose stored link points at record 1, not the
    # record actually preceding it.
    assert result.broken_at == 1


def test_dropping_the_head_is_detected_at_the_new_genesis() -> None:
    """Truncating the *front* leaves a first record carrying a predecessor."""
    result = verify_chain(_ledger(4)[1:])
    assert result.ok is False
    assert result.broken_at == 0
    assert result.reason is not None and "genesis" in result.reason


def test_truncating_the_tail_leaves_the_chain_internally_consistent() -> None:
    """The honest limit of a bare chain — and why the facet binds the head.

    Every remaining link still verifies, so `verify_chain` alone cannot see the
    removal. Recorded as a test rather than left implicit so nobody later reads
    a green chain walk as proof that nothing was dropped.
    """
    assert verify_chain(_ledger(4)[:-1]).ok is True


def test_truncating_the_tail_is_detected_by_the_facet_binding() -> None:
    records = _ledger(4)
    facet = facet_for_ledger(STORE, records)
    assert facet is not None
    assert verify_facet_binding(facet, records) is True
    assert verify_facet_binding(facet, records[:-1]) is False


def test_verification_reports_a_position_not_just_a_verdict() -> None:
    """An auditor holding ten thousand records needs the index."""
    records = _ledger(6)
    records[4] = records[4].model_copy(update={"op": "delete"})
    result = verify_chain(records)
    assert result.ok is False
    assert result.broken_at == 5


def test_append_only_guard_accepts_an_appended_ledger() -> None:
    before = _ledger(3)
    after = append_mutation(
        before,
        store_id=STORE,
        namespace="playbooks",
        entry_id="pb-5000",
        op="create",
        value_digest=digest_value(CONTENT_V2),
        by=_actor(),
        at="2026-07-13T11:00:00Z",
    )
    verify_append_only(before, after)


def test_append_only_guard_rejects_a_shrunken_ledger() -> None:
    before = _ledger(3)
    with pytest.raises(LedgerRewriteError, match="append-only"):
        verify_append_only(before, before[:-1])


def test_append_only_guard_rejects_an_edited_prior_record() -> None:
    """Rewriting `at` or `by` is the same falsification as rewriting the op,
    and is harder to spot because the digests still read correctly."""
    before = _ledger(3)
    after = list(before)
    after[1] = after[1].model_copy(update={"by": _actor(agent="someone-else")})
    with pytest.raises(LedgerRewriteError, match="record 1 was rewritten"):
        verify_append_only(before, after)


def test_append_mutation_does_not_mutate_the_input_ledger() -> None:
    before = _ledger(2)
    snapshot = list(before)
    append_mutation(
        before,
        store_id=STORE,
        namespace="playbooks",
        entry_id="pb-6000",
        op="create",
        value_digest=digest_value(CONTENT_V1),
        by=_actor(),
        at="2026-07-13T12:00:00Z",
    )
    assert before == snapshot


def test_chain_head_of_an_empty_ledger_is_none() -> None:
    """Not the digest of nothing: a store with no recorded history must not get
    a binding indistinguishable from one whose history was erased."""
    assert chain_head([]) is None


# ── I-2: who / what / when only, never store content ──────────────────────


def test_entry_content_never_enters_the_record() -> None:
    records = _ledger(1)
    record = append_mutation(
        records,
        store_id=STORE,
        namespace="playbooks",
        entry_id="pb-4471",
        op="update",
        value_digest=digest_value(CONTENT_V2),
        prev_value_digest=digest_value(CONTENT_V1),
        by=_actor(),
        at="2026-07-13T09:14:02Z",
    )[-1]
    dumped = record.model_dump_json()
    assert CONTENT_V1 not in dumped
    assert CONTENT_V2 not in dumped
    assert digest_value(CONTENT_V2) in dumped


def test_raw_bytes_are_rejected_rather_than_hashed_for_the_caller() -> None:
    """Hashing here would make it effortless to hand this module the entry
    itself and have it quietly do the right-looking thing."""
    with pytest.raises(ContentCaptureError, match="raw bytes"):
        MutationRecord(
            store_id=STORE,
            namespace="playbooks",
            entry_id="pb-1",
            op="create",
            value_digest=CONTENT_V1.encode(),  # type: ignore[arg-type]
            by=_actor(),
            at="2026-07-13T09:00:00Z",
        )


def test_an_embedding_vector_is_rejected() -> None:
    with pytest.raises(ContentCaptureError, match="vector or embedding"):
        MutationRecord(
            store_id=STORE,
            namespace="playbooks",
            entry_id="pb-1",
            op="create",
            value_digest=[0.12, 0.98, 0.4],  # type: ignore[arg-type]
            by=_actor(),
            at="2026-07-13T09:00:00Z",
        )


def test_an_over_long_identifier_is_rejected_as_inlined_content() -> None:
    with pytest.raises(ContentCaptureError, match="identifier limit"):
        MutationRecord(
            store_id=STORE,
            namespace="playbooks",
            entry_id="x" * 4096,
            op="create",
            value_digest=digest_value(CONTENT_V1),
            by=_actor(),
            at="2026-07-13T09:00:00Z",
        )


def test_a_malformed_digest_is_rejected_at_construction() -> None:
    with pytest.raises(InvalidDigestError, match="sha256"):
        MutationRecord(
            store_id=STORE,
            namespace="playbooks",
            entry_id="pb-1",
            op="create",
            value_digest="md5:abc",
            by=_actor(),
            at="2026-07-13T09:00:00Z",
        )


def test_a_locator_is_not_accepted_as_a_value_binding() -> None:
    """A URI names where something lives, not what it is — and a store entry is
    exactly the kind of thing replaced underneath its own id."""
    with pytest.raises(InvalidDigestError):
        MutationRecord(
            store_id=STORE,
            namespace="playbooks",
            entry_id="pb-1",
            op="create",
            value_digest="https://kb.example.org/entries/pb-1",
            by=_actor(),
            at="2026-07-13T09:00:00Z",
        )


def test_scan_for_content_surfaces_a_payload_before_construction() -> None:
    with pytest.raises(ContentCaptureError):
        scan_for_content([STORE, CONTENT_V1.encode()])


def test_a_mutation_with_no_time_is_rejected() -> None:
    with pytest.raises(ValueError):
        MutationRecord(
            store_id=STORE,
            namespace="playbooks",
            entry_id="pb-1",
            op="create",
            value_digest=digest_value(CONTENT_V1),
            by=_actor(),
            at="   ",
        )


def test_an_unknown_op_is_rejected() -> None:
    with pytest.raises(ValueError):
        MutationRecord(
            store_id=STORE,
            namespace="playbooks",
            entry_id="pb-1",
            op="obliterate",  # type: ignore[arg-type]
            value_digest=digest_value(CONTENT_V1),
            by=_actor(),
            at="2026-07-13T09:00:00Z",
        )


# ── I-1: record-only, store-external ──────────────────────────────────────


def test_a_delete_may_not_carry_a_post_op_value_digest() -> None:
    """Recording a digest for a removed entry would assert the store holds
    something it does not."""
    with pytest.raises(MemstoreError, match="leaves no value"):
        append_mutation(
            [],
            store_id=STORE,
            namespace="playbooks",
            entry_id="pb-1",
            op="delete",
            value_digest=digest_value(""),
            by=_actor(),
            at="2026-07-13T09:00:00Z",
        )


def test_a_delete_records_only_the_prior_digest() -> None:
    record = append_mutation(
        [],
        store_id=STORE,
        namespace="playbooks",
        entry_id="pb-1",
        op="delete",
        prev_value_digest=digest_value(CONTENT_V1),
        by=_actor(),
        at="2026-07-13T09:00:00Z",
    )[0]
    assert record.value_digest is None
    assert record.prev_value_digest == digest_value(CONTENT_V1)


def test_a_mutation_without_a_run_is_recorded_not_invented() -> None:
    """A scheduled compaction or a console edit has no NovaFabric run."""
    record = append_mutation(
        [],
        store_id=STORE,
        namespace="playbooks",
        entry_id="pb-1",
        op="supersede",
        value_digest=digest_value(CONTENT_V2),
        by=MutationActor(agent="retention-compactor"),
        at="2026-07-13T09:00:00Z",
    )[0]
    assert record.by.run is None


def test_module_exposes_no_store_mutating_surface() -> None:
    """Store-external is a property of the API, not just of the docs.

    If a write/put/delete/apply entry point ever appears here, this fails —
    which is the point: I-1 should be structurally hard to violate.
    """
    import novafabric.memstore as memstore

    forbidden = {"write", "put", "delete", "apply", "commit", "rollback", "purge"}
    assert forbidden.isdisjoint({name.lower() for name in memstore.__all__})


# ── I-3: additive-first and fail-open ─────────────────────────────────────


def test_a_store_less_capsule_is_returned_unchanged(capsule: dict[str, Any]) -> None:
    """Byte-identical to a capsule captured before this feature existed."""
    before = json.dumps(capsule, sort_keys=True)
    out = attach_facet(capsule, None)
    assert out == capsule
    assert json.dumps(out, sort_keys=True) == before


def test_an_empty_ledger_produces_no_facet_at_all() -> None:
    """An empty facet would assert "this run recorded that it changed nothing" —
    a claim nobody made. Absent is not false."""
    assert facet_for_ledger(STORE, []) is None


def test_attach_does_not_mutate_the_input_capsule() -> None:
    capsule: dict[str, Any] = {"run_id": "r"}
    attach_facet(capsule, build_facet(STORE, _ledger(1)))
    assert capsule == {"run_id": "r"}


def test_attach_preserves_sibling_facets() -> None:
    capsule = {"run_id": "r", "facets": {"safety": {"schema_version": "0.1.0"}}}
    out = attach_facet(capsule, build_facet(STORE, _ledger(1)))
    assert out["facets"]["safety"] == {"schema_version": "0.1.0"}
    assert "memstore_mutation" in out["facets"]


def test_facet_round_trips_through_a_capsule_and_still_verifies() -> None:
    records = _ledger(3)
    facet = facet_for_ledger(STORE, records)
    out = attach_facet({"run_id": "r"}, facet)
    read_back = facet_from_capsule(out)
    assert read_back is not None
    assert verify_chain(read_back.records_in_this_run).ok is True
    assert verify_facet_binding(read_back, records) is True


def test_facet_from_a_capsule_without_one_is_none() -> None:
    assert facet_from_capsule({"run_id": "r"}) is None
    assert facet_from_capsule({"run_id": "r", "facets": {}}) is None


def test_facet_preserves_record_order_verbatim() -> None:
    """Sorting by timestamp would silently repair a ledger whose records
    arrived out of order — hiding exactly the anomaly an auditor wants."""
    records = _ledger(3)
    shuffled = [records[2], records[0], records[1]]
    facet = build_facet(STORE, shuffled)
    assert [r.entry_id for r in facet.records_in_this_run] == [
        records[2].entry_id,
        records[0].entry_id,
        records[1].entry_id,
    ]


def test_facet_carries_a_schema_version() -> None:
    out = attach_facet({"run_id": "r"}, build_facet(STORE, _ledger(1)))
    assert out["facets"]["memstore_mutation"]["schema_version"]


def test_absent_ledger_ref_does_not_verify_as_bound() -> None:
    """An unbound facet is the case the binding check exists to surface."""
    facet = build_facet(STORE, _ledger(1))
    assert facet.ledger_ref is None
    assert verify_facet_binding(facet, _ledger(1)) is False


def test_serialised_facet_omits_absent_fields_rather_than_nulling_them() -> None:
    out = attach_facet({"run_id": "r"}, facet_for_ledger(STORE, _ledger(1)))
    block = out["facets"]["memstore_mutation"]
    assert "run" in block["records_in_this_run"][0]["by"]
    assert "prev_value_digest" not in block["records_in_this_run"][0]
    assert block["verified"] == {"ok": True}


# ── Real-schema validation (ADR-0196 boundary) ────────────────────────────


def test_store_less_capsule_fixture_validates(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    """Golden fixture 1: the store-less capsule is still valid."""
    assert "facets" not in capsule
    jsonschema.validate(capsule, schema)


def test_facet_bearing_capsule_validates_against_the_real_schema(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    """The regression ADR-0196 exists to prevent.

    Uses the shipped builder rather than a hand-written dict, because a
    hand-written dict is what let the original gap through.
    """
    out = attach_facet(capsule, facet_for_ledger(STORE, _ledger(3)))
    assert "memstore_mutation" in out["facets"]
    jsonschema.validate(out, schema)


def test_facet_name_matches_the_schema_registry() -> None:
    from novafabric.memstore import FACET_NAME

    registered = json.loads(SCHEMA_PATH.read_text())["properties"]["facets"][
        "properties"
    ]
    assert FACET_NAME in registered


# ── Golden fixture: a valid ledger ────────────────────────────────────────


def test_valid_ledger_fixture_verifies() -> None:
    """Golden fixture 2: a valid ledger, pinned on disk.

    This is the drift guard for the hash preimage. The fixture's
    `prev_record_hash` values were computed by an earlier version of this
    module; if the canonicalisation, the field set, or the digest construction
    ever changes, the recomputed links stop matching and this fails — which is
    what stops a silent, chain-breaking format change from shipping.
    """
    doc = json.loads(VALID_LEDGER.read_text())
    records = [MutationRecord.model_validate(r) for r in doc["ledger"]]
    result = verify_chain(records)
    assert result.ok is True, result.reason


def test_valid_ledger_fixture_binds_to_its_facet() -> None:
    doc = json.loads(VALID_LEDGER.read_text())
    records = [MutationRecord.model_validate(r) for r in doc["ledger"]]
    facet = MemstoreMutationFacet.model_validate(doc["facet"])
    assert verify_facet_binding(facet, records) is True


def test_valid_ledger_fixture_carries_no_store_content() -> None:
    raw = VALID_LEDGER.read_text()
    for record in json.loads(raw)["ledger"]:
        assert set(record) <= {
            "schema_version",
            "store_id",
            "namespace",
            "entry_id",
            "op",
            "value_digest",
            "prev_value_digest",
            "by",
            "at",
            "prev_record_hash",
        }


def test_valid_ledger_fixture_attached_to_a_capsule_validates(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    doc = json.loads(VALID_LEDGER.read_text())
    facet = MemstoreMutationFacet.model_validate(doc["facet"])
    jsonschema.validate(attach_facet(capsule, facet), schema)
