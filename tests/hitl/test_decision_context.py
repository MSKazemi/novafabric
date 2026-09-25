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

"""ADR-0150 P2 — decision-context receipt (NF-182) and shared record plumbing.

Organised by the D7 invariants: digest-only, pseudonymous, fail-open recording,
fail-closed verification, and the Merkle ``context_root`` re-performance
guarantee (ADR-0087).
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from novafabric.evidence.merkle import _leaf, _merkle_root
from novafabric.hitl import (
    AccountabilityRecordError,
    DecisionContextReceipt,
    IdentityRefError,
    RecordContentError,
    ShownItem,
    _records,
    build_decision_context,
    compute_context_root,
    digest_turn,
    receipts_for_turn,
    record_decision_context,
    shown_item,
    verify_decision_contexts,
)
from novafabric.hitl.decision_context import MAX_SHOWN_ITEMS

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "run-capsule.schema.json"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "conversation"
THREADED = FIXTURES / "threaded-capsule.json"
ACCOUNTABILITY = FIXTURES / "accountability-capsule.json"
TAMPERED = FIXTURES / "invalid-tampered-context-capsule.json"
DANGLING = FIXTURES / "invalid-dangling-ref-capsule.json"
TEXT_ONLY = FIXTURES / "valid-text-only-capsule.json"

DECIDER = "human:fp:9f2c4a1b7e0d5638"
AT = "2026-07-15T10:00:31Z"


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def _items() -> list[ShownItem]:
    return [
        shown_item("tool_output", content="db error rate 4.1%", rendered_at=AT),
        shown_item("score", item_digest=digest_turn("risk=0.12"), rendered_at=AT),
    ]


def _receipt(**kw: Any) -> DecisionContextReceipt:
    args: dict[str, Any] = {
        "decision": "approve",
        "reason": "risk_within_tolerance",
        "decided_by": DECIDER,
    }
    args.update(kw)
    return build_decision_context("t2", _items(), **args)


# ── Golden fixtures against the real schema ───────────────────────────────


@pytest.mark.parametrize("path", [ACCOUNTABILITY, TAMPERED, DANGLING])
def test_fixtures_validate_against_run_capsule_schema(path: Path) -> None:
    """Records nest inside the open ``facets.conversation`` object, so even the
    invalid fixtures are *schema*-valid — the defect is caught by verify."""
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.validate(_load(path), schema)


def test_golden_capsule_verifies_ok() -> None:
    result = verify_decision_contexts(_load(ACCOUNTABILITY))
    assert result.status == "ok"
    assert [v.turn_ref for v in result.verdicts] == ["t2"]


def test_tampered_shown_item_breaks_the_root() -> None:
    result = verify_decision_contexts(_load(TAMPERED))
    assert result.status == "defective"
    assert not result.verdicts[0].root_matches


def test_dangling_turn_ref_is_defective() -> None:
    result = verify_decision_contexts(_load(DANGLING))
    assert result.status == "defective"
    assert result.verdicts[0].turn_resolves is False


def test_capsule_without_receipts_is_empty() -> None:
    assert verify_decision_contexts(_load(THREADED)).status == "empty"
    assert verify_decision_contexts(_load(TEXT_ONLY)).status == "empty"


# ── Merkle root ───────────────────────────────────────────────────────────


def test_root_uses_the_evidence_merkle_construction() -> None:
    items = _items()
    leaves = [
        _leaf(
            json.dumps(
                {
                    "item_digest": i.item_digest,
                    "item_kind": i.item_kind,
                    "rendered_at": i.rendered_at,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        for i in items
    ]
    assert compute_context_root(items) == "sha256:" + _merkle_root(leaves).hex()


def test_root_is_order_sensitive() -> None:
    items = _items()
    assert compute_context_root(items) != compute_context_root(list(reversed(items)))


def test_single_item_root_is_its_leaf() -> None:
    item = _items()[0]
    assert compute_context_root([item]) == "sha256:" + _leaf(item.leaf_bytes()).hex()


def test_empty_and_oversize_shown_context_are_refused() -> None:
    with pytest.raises(AccountabilityRecordError):
        compute_context_root([])
    item = _items()[0]
    with pytest.raises(AccountabilityRecordError):
        compute_context_root([item] * (MAX_SHOWN_ITEMS + 1))


def test_shown_content_is_hashed_and_discarded() -> None:
    item = shown_item("warning", content="error budget exhausted", rendered_at=AT)
    assert "error budget" not in item.model_dump_json()
    expected = "sha256:" + hashlib.sha256(b"error budget exhausted").hexdigest()
    assert item.item_digest == expected


def test_shown_item_requires_exactly_one_source() -> None:
    with pytest.raises(AccountabilityRecordError):
        shown_item("score", rendered_at=AT)
    with pytest.raises(AccountabilityRecordError):
        shown_item("score", content="x", item_digest=digest_turn("x"), rendered_at=AT)


def test_shown_item_forbids_unbound_extras() -> None:
    with pytest.raises(Exception, match="extra"):
        ShownItem.model_validate(
            {"item_kind": "score", "item_digest": digest_turn("x"), "rendered_at": AT, "note": "n"}
        )


@pytest.mark.parametrize("kind", ["Tool Output", "", "x" * 65])
def test_bad_item_kind_is_refused(kind: str) -> None:
    with pytest.raises(AccountabilityRecordError):
        shown_item(kind, content="x", rendered_at=AT)


# ── D7: digest-only, pseudonymous ─────────────────────────────────────────


def test_prose_reason_is_refused_but_its_digest_is_accepted() -> None:
    prose = "Alice thought the risk was fine"
    with pytest.raises(RecordContentError) as excinfo:
        _receipt(reason=prose)
    assert prose not in str(excinfo.value)
    assert _receipt(reason=digest_turn(prose)).reason == digest_turn(prose)


def test_decider_must_be_a_pseudonymous_human_ref() -> None:
    with pytest.raises(IdentityRefError):
        _receipt(decided_by="human:alice@example.com")
    with pytest.raises(IdentityRefError):
        _receipt(decided_by="agent:spiffe://acme.example/sa/bot")


@pytest.mark.parametrize(
    "extra",
    [{"reason_text": "x"}, {"prompt": "x"}, {"note": {"nested": 1}}, {"note": "x" * 600}],
)
def test_payload_shaped_extensions_are_refused(extra: dict[str, Any]) -> None:
    data = _receipt().model_dump()
    data.update(extra)
    with pytest.raises(RecordContentError):
        DecisionContextReceipt.model_validate(data)


def test_bounded_scalar_extension_is_allowed() -> None:
    data = _receipt().model_dump()
    data.update({"ui_version": "3.1", "latency_ms": 12, "flag": None})
    assert DecisionContextReceipt.model_validate(data).model_extra == {
        "ui_version": "3.1",
        "latency_ms": 12,
        "flag": None,
    }


@pytest.mark.parametrize(
    ("field", "value", "exc"),
    [
        ("turn_ref", "", AccountabilityRecordError),
        ("turn_ref", "has space", RecordContentError),
        ("turn_ref", "t" * 300, RecordContentError),
        ("decision", "Approve it", RecordContentError),
        ("context_root", b"raw", RecordContentError),
        ("context_root", "sha256:abc", RecordContentError),
        ("decided_at", "yesterday", Exception),
        ("decided_at", 5, AccountabilityRecordError),
        ("nf086_approval_ref", "not-a-digest", RecordContentError),
    ],
)
def test_malformed_fields_raise_named_errors(field: str, value: Any, exc: type[Exception]) -> None:
    data = _receipt().model_dump()
    data[field] = value
    with pytest.raises(exc):
        DecisionContextReceipt.model_validate(data)


def test_nf086_reference_is_carried_not_duplicated() -> None:
    ref = digest_turn("nf086-approval")
    receipt = _receipt(nf086_approval_ref=ref, decided_at=AT)
    assert receipt.nf086_approval_ref == ref
    assert "approver_identity" not in receipt.model_dump()


# ── Fail-open recording ───────────────────────────────────────────────────


def test_record_appends_inside_conversation_facet() -> None:
    capsule = _load(THREADED)
    before = copy.deepcopy(capsule)
    outcome = record_decision_context(capsule, _receipt())
    assert outcome.recorded
    assert capsule == before, "input must not be mutated"
    stored = outcome.capsule["facets"]["conversation"]["decision_context"]
    assert stored[0]["turn_ref"] == "t2"
    assert verify_decision_contexts(outcome.capsule).status == "ok"
    assert [i for i, _ in receipts_for_turn(outcome.capsule, "t2")] == [0]


def test_record_accepts_a_mapping() -> None:
    outcome = record_decision_context(_load(THREADED), _receipt().model_dump())
    assert outcome.recorded


@pytest.mark.parametrize(
    ("capsule", "receipt", "reason"),
    [
        ({"run_id": "x"}, None, "no conversation facet"),
        (None, {"turn_ref": "t9"}, "turn_ref does not resolve"),
        (None, {"decided_by": "alice"}, "IdentityRefError"),
        (None, {"reason": "free text reason"}, "RecordContentError"),
        (None, {"context_root": digest_turn("forged")}, "context_root does not match"),
    ],
)
def test_record_never_raises(
    capsule: dict[str, Any] | None, receipt: dict[str, Any] | None, reason: str
) -> None:
    cap = capsule if capsule is not None else _load(THREADED)
    data = _receipt().model_dump()
    data.update(receipt or {})
    outcome = record_decision_context(cap, data)
    assert not outcome.recorded
    assert outcome.capsule is cap
    assert outcome.reason is not None and reason in outcome.reason


def test_second_receipt_for_same_turn_is_not_recorded() -> None:
    first = record_decision_context(_load(THREADED), _receipt())
    second = record_decision_context(first.capsule, _receipt(decision="reject"))
    assert not second.recorded
    assert "already recorded" in (second.reason or "")


def test_malformed_capsule_facet_does_not_raise() -> None:
    capsule = _load(THREADED)
    capsule["facets"]["conversation"]["turns"] = "not-a-list"
    outcome = record_decision_context(capsule, _receipt())
    assert not outcome.recorded


def test_existing_non_list_and_cap_are_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    capsule = _load(THREADED)
    capsule["facets"]["conversation"]["decision_context"] = {"bad": 1}
    assert "not a list" in (record_decision_context(capsule, _receipt()).reason or "")
    monkeypatch.setattr(_records, "MAX_RECORDS_PER_KIND", 1)
    capsule = record_decision_context(_load(THREADED), _receipt()).capsule
    receipt = build_decision_context(
        "t0", _items(), decision="approve", reason="ok", decided_by=DECIDER
    )
    assert "cap reached" in (record_decision_context(capsule, receipt).reason or "")


# ── Fail-closed verification ──────────────────────────────────────────────


def test_malformed_stored_receipt_is_a_defect_not_skipped() -> None:
    capsule = _load(ACCOUNTABILITY)
    capsule["facets"]["conversation"]["decision_context"].append({"turn_ref": "t1"})
    result = verify_decision_contexts(capsule)
    assert result.status == "defective"
    assert result.defects[0].index == 1


def test_non_list_and_oversize_stored_lists_are_defects(monkeypatch: pytest.MonkeyPatch) -> None:
    capsule = _load(ACCOUNTABILITY)
    capsule["facets"]["conversation"]["decision_context"] = "x"
    assert verify_decision_contexts(capsule).defects[0].index == -1
    capsule = _load(ACCOUNTABILITY)
    stored = capsule["facets"]["conversation"]["decision_context"]
    stored.append(copy.deepcopy(stored[0]))
    monkeypatch.setattr(_records, "MAX_RECORDS_PER_KIND", 1)
    result = verify_decision_contexts(capsule)
    assert result.status == "defective"
    assert "exceeds" in result.defects[0].error


def test_duplicate_receipts_and_duplicate_turn_ids_are_defective() -> None:
    capsule = _load(ACCOUNTABILITY)
    stored = capsule["facets"]["conversation"]["decision_context"]
    stored.append(copy.deepcopy(stored[0]))
    result = verify_decision_contexts(capsule)
    assert result.status == "defective"
    assert all(v.duplicate_for_turn for v in result.verdicts)

    capsule = _load(ACCOUNTABILITY)
    turns = capsule["facets"]["conversation"]["turns"]
    turns.append({**turns[2], "at": "2026-07-15T10:00:40Z"})
    result = verify_decision_contexts(capsule)
    assert result.status == "defective"
    assert result.duplicate_turn_ids == ["t2"]


def test_malformed_conversation_facet_is_a_defect() -> None:
    capsule = _load(ACCOUNTABILITY)
    capsule["facets"]["conversation"]["turns"][0]["author"] = "alice@example.com"
    result = verify_decision_contexts(capsule)
    assert result.status == "defective"
    assert any("conversation facet" in d.error for d in result.defects)


def test_verdict_to_dict_is_deterministic() -> None:
    verdict = verify_decision_contexts(_load(ACCOUNTABILITY)).verdicts[0]
    assert verdict.to_dict() == verdict.to_dict()
    assert verdict.to_dict()["ok"] is True
