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

"""ADR-0143 P2 — context receipt (NF-113) and grounding map (NF-112)."""

from __future__ import annotations

import hashlib
import json

import pytest

from novafabric.capture.events import RetrievedDocument, VectorRetrievalEvent
from novafabric.context import (
    ChunkRef,
    ContentCaptureError,
    ContextEntry,
    ContextProvenanceError,
    ContextReceipt,
    GroundingMap,
    InvalidReferenceError,
    ReceiptOrderError,
    TokenBudget,
    build_grounding,
    build_receipt,
    chunk_refs_from_event,
    digest_content,
    entry_from_content,
    ground_span,
    is_recorded,
    receipt_digest,
    receipt_preimage,
    support_for,
    verify_receipt_digest,
)

CAPSULE = "01J0000000000000000000CAPS"
RUN = "01J0000000000000000000RUN0"


def _entry(source: str, position: int, tokens: int = 10, body: str = "x") -> ContextEntry:
    return ContextEntry(
        source=source,  # type: ignore[arg-type]
        digest=digest_content(f"{body}-{position}"),
        token_count=tokens,
        position=position,
    )


def _retrieval(*documents: RetrievedDocument, operation: str = "query") -> VectorRetrievalEvent:
    return VectorRetrievalEvent(
        run_id=RUN,
        capsule_id=CAPSULE,
        timestamp_utc="2026-07-20T10:00:00Z",
        vector_store="qdrant",
        operation=operation,  # type: ignore[arg-type]
        documents=list(documents),
    )


# ── NF-113: ordered manifest ──────────────────────────────────────────────


def test_manifest_keeps_caller_order_and_is_never_sorted() -> None:
    """Order is the evidence: the manifest comes back exactly as given."""
    entries = [
        _entry("history", 0),
        _entry("system", 1),
        _entry("retrieved", 2),
        _entry("user", 3),
    ]
    receipt = build_receipt(entries)

    assert [e.source for e in receipt.entries] == [
        "history",
        "system",
        "retrieved",
        "user",
    ]
    assert [e.position for e in receipt.entries] == [0, 1, 2, 3]


def test_position_disagreeing_with_list_order_is_rejected() -> None:
    with pytest.raises(ReceiptOrderError):
        ContextReceipt(
            entries=[_entry("system", 0), _entry("user", 7)],
            token_budget=TokenBudget(used=0),
        )


def test_reordering_entries_changes_the_receipt_digest() -> None:
    """The whole point of binding order: a swap is detectable offline."""
    a, b = _entry("system", 0, body="alpha"), _entry("user", 1, body="beta")
    first = build_receipt([a, b])

    # Re-position the same two items in the opposite order.
    swapped = build_receipt(
        [
            b.model_copy(update={"position": 0}),
            a.model_copy(update={"position": 1}),
        ]
    )

    assert first.receipt_digest != swapped.receipt_digest


# ── NF-113: token budget ──────────────────────────────────────────────────


def test_token_budget_is_derived_from_the_manifest() -> None:
    receipt = build_receipt(
        [
            _entry("system", 0, tokens=5),
            _entry("retrieved", 1, tokens=100),
            _entry("retrieved", 2, tokens=40),
            _entry("memory", 3, tokens=7),
        ],
        window=8192,
    )

    assert receipt.token_budget.used == 152
    assert receipt.token_budget.window == 8192
    assert receipt.token_budget.by_source == {
        "system": 5,
        "retrieved": 140,
        "memory": 7,
    }


def test_absent_window_stays_none_rather_than_zero() -> None:
    """An unknown window is not an unlimited one, and not a full one."""
    receipt = build_receipt([_entry("system", 0)])
    assert receipt.token_budget.window is None
    assert "window" not in receipt_preimage(receipt)


def test_by_source_omits_sources_with_no_entry() -> None:
    receipt = build_receipt([_entry("system", 0, tokens=3)])
    assert receipt.token_budget.by_source == {"system": 3}


# ── NF-113: receipt_digest is plain sha256, pinned ────────────────────────


def test_receipt_digest_is_bit_identical_to_raw_hashlib_sha256() -> None:
    """Pins the construction: plain sha256 over canonical JSON, no Merkle tree.

    If anyone ever routes this through evidence/merkle.py (RFC 6962) or
    trust/novaseal/merkle.py (pairwise+pad), this fails — which is the point.
    """
    receipt = build_receipt([_entry("system", 0), _entry("user", 1)], window=4096)

    preimage = receipt_preimage(receipt)
    expected = "sha256:" + hashlib.sha256(preimage.encode("utf-8")).hexdigest()

    assert receipt.receipt_digest == expected
    assert receipt_digest(receipt) == expected


def test_preimage_is_canonical_json_excluding_the_digest_itself() -> None:
    receipt = build_receipt([_entry("system", 0)])
    preimage = receipt_preimage(receipt)

    assert preimage == json.dumps(
        json.loads(preimage), separators=(",", ":"), sort_keys=True
    )
    assert "receipt_digest" not in preimage


def test_verify_receipt_digest_detects_a_tampered_manifest() -> None:
    receipt = build_receipt([_entry("system", 0, tokens=10)])
    assert verify_receipt_digest(receipt) is True

    receipt.entries[0].token_count = 999
    assert verify_receipt_digest(receipt) is False


def test_unbound_receipt_does_not_verify() -> None:
    """An unsealed manifest must not read as a checked one."""
    receipt = ContextReceipt(
        entries=[_entry("system", 0)], token_budget=TokenBudget(used=10)
    )
    assert receipt.receipt_digest is None
    assert verify_receipt_digest(receipt) is False


def test_empty_manifest_is_valid_and_bindable() -> None:
    receipt = build_receipt([])
    assert receipt.entries == []
    assert receipt.token_budget.used == 0
    assert verify_receipt_digest(receipt) is True


# ── I-2: no content anywhere ──────────────────────────────────────────────


def test_raw_bytes_in_a_digest_field_raise_content_capture_error() -> None:
    with pytest.raises(ContentCaptureError):
        ContextEntry(
            source="user",
            digest=b"the actual prompt text",  # type: ignore[arg-type]
            token_count=4,
            position=0,
        )


def test_embedding_vector_in_a_digest_field_is_rejected() -> None:
    with pytest.raises(ContentCaptureError):
        ContextEntry(
            source="retrieved",
            digest=[0.12, 0.98, 0.4],  # type: ignore[arg-type]
            token_count=4,
            position=0,
        )


def test_plain_text_in_a_digest_field_is_rejected() -> None:
    with pytest.raises(InvalidReferenceError):
        ContextEntry(
            source="system",
            digest="You are a helpful assistant.",
            token_count=6,
            position=0,
        )


def test_entry_from_content_hashes_and_discards_the_text() -> None:
    secret = "PATIENT: Jane Doe, MRN 12345"
    entry = entry_from_content(secret, source="memory", position=0, token_count=9)

    assert entry.digest == digest_content(secret)
    assert secret not in entry.model_dump_json()


def test_chunk_body_never_enters_the_grounding_map() -> None:
    document = RetrievedDocument(
        document_id="doc-17",
        score=0.91,
        content="the confidential chunk body",
        content_hash=digest_content("the confidential chunk body"),
    )
    refs = chunk_refs_from_event(_retrieval(document))

    assert len(refs) == 1
    assert "confidential" not in refs[0].model_dump_json()


def test_named_exceptions_do_not_subclass_value_error() -> None:
    for exc in (ContextProvenanceError, InvalidReferenceError, ContentCaptureError,
                ReceiptOrderError):
        assert issubclass(exc, Exception)
        assert not issubclass(exc, ValueError)


# ── NF-112: chunk refs from the shipped event ─────────────────────────────


def test_chunk_refs_take_rank_from_retrieval_order() -> None:
    refs = chunk_refs_from_event(
        _retrieval(
            RetrievedDocument(document_id="doc-a", score=0.4),
            RetrievedDocument(document_id="doc-b", score=0.9),
        )
    )
    # Rank follows the store's returned order, NOT the score — the model saw
    # them in this order.
    assert [(r.document_id, r.rank) for r in refs] == [("doc-a", 1), ("doc-b", 2)]


def test_missing_content_hash_stays_none_meaning_unknown() -> None:
    refs = chunk_refs_from_event(_retrieval(RetrievedDocument(document_id="doc-old")))
    assert refs[0].content_hash is None


def test_adr_0153_content_hash_is_carried_through() -> None:
    pin = digest_content("chunk body v2")
    refs = chunk_refs_from_event(
        _retrieval(RetrievedDocument(document_id="doc-9", content_hash=pin))
    )
    assert refs[0].content_hash == pin


def test_non_query_operations_yield_no_chunks() -> None:
    event = _retrieval(RetrievedDocument(document_id="doc-1"), operation="upsert")
    assert chunk_refs_from_event(event) == []


def test_retrieval_similarity_score_is_not_carried_as_support() -> None:
    """A store similarity score must not masquerade as a support strength."""
    refs = chunk_refs_from_event(
        _retrieval(RetrievedDocument(document_id="doc-1", score=0.99))
    )
    assert "score" not in refs[0].model_dump()


# ── NF-112: absent is not false ───────────────────────────────────────────


def test_unrecorded_span_returns_none_not_a_negative_finding() -> None:
    grounding = build_grounding(
        [ground_span("out:para-1", support="supported", chunks=[ChunkRef(document_id="d1")])]
    )

    assert support_for(grounding, "out:para-2") is None
    assert is_recorded(grounding, "out:para-2") is False


def test_uncited_is_recorded_and_distinct_from_absent() -> None:
    """A recorded 'cited nothing' is a different fact from 'nothing recorded'."""
    grounding = build_grounding([ground_span("out:para-3", support="uncited")])

    recorded = support_for(grounding, "out:para-3")
    assert recorded is not None
    assert recorded.support == "uncited"
    assert recorded.supported_by == []
    assert is_recorded(grounding, "out:para-3") is True

    # ... while a span nobody recorded is absent, not uncited.
    assert is_recorded(grounding, "out:para-4") is False


def test_empty_grounding_map_asserts_nothing_about_any_span() -> None:
    empty = GroundingMap()
    assert is_recorded(empty, "out:para-1") is False
    assert support_for(empty, "out:para-1") is None


def test_span_maps_to_its_supporting_chunks() -> None:
    pin = digest_content("supporting chunk")
    grounding = build_grounding(
        [
            ground_span(
                "out:para-2",
                support="partial",
                chunks=[ChunkRef(document_id="doc-17", content_hash=pin, rank=1)],
            )
        ]
    )

    span = support_for(grounding, "out:para-2")
    assert span is not None
    assert span.support == "partial"
    assert [(c.document_id, c.content_hash) for c in span.supported_by] == [("doc-17", pin)]


# ── NF-112: recorded, not scored ──────────────────────────────────────────


def test_module_exposes_no_scoring_entry_point() -> None:
    """Recorded-not-scored is a property of the API, not just of the docs.

    If a score/judge/verdict entry point ever appears here, this fails — which
    is the point: ADR-0143 D2 should be structurally hard to violate.
    """
    import novafabric.context as context
    import novafabric.context.grounding as grounding

    forbidden = {
        "score",
        "faithfulness",
        "entailment",
        "verdict",
        "grade",
        "rate",
        "judge",
        "adjudicate",
        "evaluate",
        "is_grounded",
        "assert_grounded",
        "groundedness",
    }
    for module in (context, grounding):
        exported = {name.lower() for name in module.__all__}
        assert forbidden.isdisjoint(exported), (
            f"{module.__name__} must not expose a scoring entry point "
            "(ADR-0143 D2 — NovaFabric records the support map, it does not "
            "adjudicate it)"
        )
        # No *callable* may merely contain a scoring verb either. Restricted to
        # callables on purpose: a scoring entry point is something a caller can
        # invoke, whereas RECORDED_NOT_SCORED_NOTE is a string constant whose
        # whole job is to say the opposite — matching it here would fail the
        # test for carrying the honesty line it exists to enforce.
        for name in module.__all__:
            if not callable(getattr(module, name)):
                continue
            assert not any(word in name.lower() for word in forbidden), name


def test_grounding_map_carries_the_recorded_not_scored_note() -> None:
    grounding = build_grounding([ground_span("out:para-1", support="supported")])
    assert "not scored" in grounding.note
    assert "not scored" in grounding.model_dump_json()


def test_support_label_is_required_and_never_defaulted() -> None:
    """A default label would be this layer inventing a claim (D2)."""
    with pytest.raises(Exception):
        ground_span("out:para-1")  # type: ignore[call-arg]


# ── Additive-first: nothing here touches a capsule ────────────────────────


def test_p2_names_are_not_in_the_closed_capsule_facet_registry() -> None:
    """P2 is a standalone artifact: the facets registry registers neither name.

    Guards the additive-first promise from the other direction — if a later
    slice registers `context_receipt`/`grounding` in the capsule schema, this
    fails and forces a deliberate decision instead of a silent divergence.
    """
    import json as _json
    from pathlib import Path

    schema_path = (
        Path(__file__).resolve().parents[2] / "schemas" / "run-capsule.schema.json"
    )
    schema = _json.loads(schema_path.read_text())
    registry = schema["properties"]["facets"]["properties"]

    assert "context_receipt" not in registry
    assert "grounding" not in registry


def test_module_exposes_no_capsule_mutation_entry_point() -> None:
    """A capsule with no context material stays byte-identical and unmutated."""
    import novafabric.context as context

    assert not hasattr(context, "attach_facet")
    assert "attach_facet" not in context.__all__
