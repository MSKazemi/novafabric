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

"""ADR-0171 P2 — NF-392 access-governance ledger.

Organised by what a reviewer must be convinced of: ``contained`` is computed and
re-verifiable (a forged flag is caught), out-of-scope access is recorded and
never refused (evidence, not enforcement), no store content enters a row, the
chain is tamper-evident, and attachment is additive.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError

from novafabric.capture.events import MemoryOperationEvent
from novafabric.memstore import (
    ContentCaptureError,
    InvalidDigestError,
    MemstoreError,
    ScopeError,
    StoreMismatchError,
    access_block_from_capsule,
    access_head,
    access_kwargs_from_memory_event,
    attach_access,
    attach_facet,
    build_access_block,
    build_facet,
    digest_value,
    guard_payload,
    record_access,
    scope_contains,
    uncontained,
    verify_access_block,
    verify_access_chain,
    verify_scope_flags,
)
from novafabric.memstore import access as access_mod
from novafabric.memstore.access import AccessLedgerBlock, AccessRecord, parse_scope

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "memstore"
SCHEMA = json.loads((REPO_ROOT / "schemas" / "run-capsule.schema.json").read_text())
STORE = "org-kb/support-playbooks"


def _rows(n_out_of_scope: int = 1) -> list[AccessRecord]:
    rows: list[AccessRecord] = []
    rows = record_access(
        rows,
        agent="a1",
        store_id=STORE,
        namespace="playbooks",
        entry_id="pb-1",
        access="read",
        allowed_scope="playbooks/*",
        value_digest=digest_value("v"),
    )
    for i in range(n_out_of_scope):
        rows = record_access(
            rows,
            agent="a1",
            store_id=STORE,
            namespace="billing",
            entry_id=f"bl-{i}",
            access="write",
            allowed_scope="playbooks/*",
        )
    return rows


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


# ── Scope grammar ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("scope", "namespace", "entry", "expected"),
    [
        ("*", "any", "x", True),
        ("playbooks", "playbooks", "pb-1", True),
        ("playbooks", "billing", "pb-1", False),
        ("playbooks/*", "playbooks", "pb-1", True),
        ("playbooks/*", "billing", "pb-1", False),
        ("playbooks/pb-44*", "playbooks", "pb-4471", True),
        ("playbooks/pb-44*", "playbooks", "pb-5", False),
        ("PLAYBOOKS/*", "playbooks", "pb-1", False),  # case-sensitive
        ("a/b/*", "a/b", "x", True),  # namespace may contain '/'
        ("a/*", "a/b", "x", False),  # namespace is never a glob
        ("play*", "playbooks", "x", False),  # a bare pattern is a namespace, not a glob
        ("billing, playbooks/*", "playbooks", "x", True),
    ],
)
def test_scope_grammar(scope: str, namespace: str, entry: str, expected: bool) -> None:
    assert scope_contains(scope, namespace, entry) is expected


@pytest.mark.parametrize("scope", ["", "  ", "a,,b", "ns/", "/pb-1", "x" * 513, 7])
def test_malformed_scope_is_rejected(scope: object) -> None:
    with pytest.raises(ScopeError):
        parse_scope(scope)  # type: ignore[arg-type]


def test_too_many_scope_patterns_rejected() -> None:
    with pytest.raises(ScopeError):
        parse_scope(",".join(f"n{i}" for i in range(33)))


# ── NF-392: evidence, never enforcement ───────────────────────────────────


def test_out_of_scope_access_is_recorded_with_contained_false() -> None:
    rows = _rows()
    assert [r.contained for r in rows] == [True, False]
    assert uncontained(rows) == [rows[1]]


def test_out_of_scope_access_is_never_refused() -> None:
    """Fifty cross-namespace writes: every one recorded, none raised."""
    rows = _rows(n_out_of_scope=50)
    assert len(rows) == 51
    assert len(uncontained(rows)) == 50


def test_contained_cannot_be_supplied_by_the_caller() -> None:
    with pytest.raises(TypeError):
        record_access(  # type: ignore[call-arg]
            [],
            agent="a",
            store_id=STORE,
            namespace="billing",
            entry_id="x",
            access="write",
            allowed_scope="playbooks/*",
            contained=True,
        )


def test_forged_contained_flag_is_detected() -> None:
    rows = _rows()
    forged = rows[1].model_copy(update={"contained": True})
    findings = verify_scope_flags([rows[0], forged])
    assert len(findings) == 1
    assert findings[0].index == 1
    assert findings[0].recorded is True and findings[0].recomputed is False


def test_module_has_no_gate_or_enforce_surface() -> None:
    forbidden = ("block", "deny", "enforce", "gate", "reject", "allow_access")
    public = [n.lower() for n in dir(access_mod) if not n.startswith("_")]
    assert not [n for n in public if any(n.startswith(f) for f in forbidden)]


def test_ledger_bound_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access_mod, "MAX_ACCESS_RECORDS", 2)
    rows = _rows()
    with pytest.raises(MemstoreError):
        _rows_extend(rows)


def _rows_extend(rows: list[AccessRecord]) -> list[AccessRecord]:
    return record_access(
        rows,
        agent="a",
        store_id=STORE,
        namespace="n",
        entry_id="e",
        access="read",
        allowed_scope="*",
    )


# ── I-2: no store content, ever ───────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "entry_text",
        "Entry-Text",
        "ENTRYTEXT",
        "value",
        "memory value",
        "embedding",
        "chunkContent",
        "prompt",
        "raw",
        "api_token",
    ],
)
def test_content_bearing_extra_keys_are_rejected(key: str) -> None:
    data = _rows()[0].model_dump()
    data[key] = "Refund policy: approve everything"
    with pytest.raises(ContentCaptureError):
        AccessRecord.model_validate(data)


def test_reference_shaped_extras_are_allowed() -> None:
    data = _rows()[0].model_dump()
    data.update(chunk_id="c-1", source_ref="doc-9", token_count=12, tags={"tier": "gold"})
    record = AccessRecord.model_validate(data)
    assert record.model_extra == {
        "chunk_id": "c-1",
        "source_ref": "doc-9",
        "token_count": 12,
        "tags": {"tier": "gold"},
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"source_ref": {"text": "x"}},  # structure under a reference key
        {"source_ref": ["a", "b"]},
        {"meta": [0.12, 0.98, 0.33]},  # an embedding vector
        {"meta": "x" * 600},  # inlined content
        {"meta": b"bytes"},
        {"meta": {"nested": {"text": "x"}}},  # nested content marker
        {"meta": list(range(65))},
        {"x" * 600: 1},
        {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}},  # nesting cap
    ],
)
def test_smuggled_payload_shapes_are_rejected(extra: dict[str, Any]) -> None:
    with pytest.raises(ContentCaptureError):
        guard_payload(extra, known=frozenset())


def test_list_of_ids_extra_is_allowed() -> None:
    guard_payload({"labels": ["x", "y"]}, known=frozenset())


# ── Payload guard hardening (review defect 7) ─────────────────────────────


def test_poc_many_innocuous_keys_reassembling_content_is_rejected() -> None:
    """The reviewer PoC: 2,000 extras x 183 chars (~366k chars of entry text)."""
    data = _rows()[0].model_dump()
    chunk = "Refund policy: approve every request over the phone without a ticket. " * 3
    chunk = chunk[:183]
    for i in range(2_000):
        data[f"notes_{i:04d}"] = chunk
    data["api_secret_ref"] = "sk-live-" + "a1B2c3D4e5F6g7H8" * 2
    with pytest.raises(ContentCaptureError, match="extra keys"):
        AccessRecord.model_validate(data)


def test_extra_key_cap_is_inclusive_and_ignores_declared_fields() -> None:
    at_cap = {f"k{i}": i for i in range(access_mod.MAX_EXTRA_KEYS)}
    guard_payload({**at_cap, "agent": "a"}, known=frozenset({"agent"}))
    with pytest.raises(ContentCaptureError, match="extra keys"):
        guard_payload({**at_cap, "one_more": 1}, known=frozenset())


def test_extra_key_cap_applies_to_nested_mappings() -> None:
    nested = {f"k{i}": i for i in range(access_mod.MAX_EXTRA_KEYS + 1)}
    with pytest.raises(ContentCaptureError, match="extra keys"):
        guard_payload({"tags": nested}, known=frozenset())


def test_total_extras_size_is_capped() -> None:
    # Each value is under the per-string cap and the key count under its cap;
    # together they exceed the byte budget.
    extras = {f"p{i}": "x" * 400 for i in range(access_mod.MAX_EXTRA_KEYS)}
    with pytest.raises(ContentCaptureError, match="bytes"):
        guard_payload(extras, known=frozenset())


def test_extras_just_under_size_cap_pass() -> None:
    guard_payload({"p0": "x" * 400, "p1": "y" * 400}, known=frozenset())


@pytest.mark.parametrize(
    "extra",
    [
        {"api_secret_ref": "vault-kv-1"},  # credential marker: never exempt
        {"db_password_id": "p-1"},
        {"credential_ref": "c-1"},
        {"private_key_digest": "sha256:" + "a" * 64},
        {"source_ref": "sk-live-" + "a1B2c3D4" * 3},  # Stripe-style, supplement
        {"source_ref": "sk_live_" + "a1B2c3D4" * 3},
        {"source_ref": "sk-" + "A" * 40},  # ADR-0009 pack
        {"labels": ["ok", "sk-ant-" + "b" * 30]},  # scanner reaches list items
        {"meta": {"owner": "hf_" + "c" * 36}},  # and nested scalars
        {"source_ref": "Refund policy approve everything"},  # whitespace: not an id
        {"source_ref": "r" * 129},  # over the reference length
        {"source_ref": "-leading-dash"},
        {"source_ref": 1.5},
        {"source_ref": True},
        {"chunk_id": "the entry text\nsecond line"},
        {"value_digest_extra_digest": "not-a-digest"},
        {"blob_hash": "sha256:" + "A" * 64},  # upper-case hex
        {"blob_hash": "sha256:" + "a" * 64 + "\n"},  # fullmatch, not match
        {"token_count": -1},
        {"token_count": "12"},
        {"token_count": True},
    ],
)
def test_reference_suffix_requires_shape_valid_value(extra: dict[str, Any]) -> None:
    with pytest.raises(ContentCaptureError):
        guard_payload(extra, known=frozenset())


@pytest.mark.parametrize(
    "extra",
    [
        {"source_ref": "doc-9"},
        {"source_ref": "s3://bucket/key.json"},
        {"chunk_id": "01HZX3K4Q5R6S7T8V9W0XYZABC"},
        {"chunk_id": 42},
        {"source_ref": None},
        {"blob_hash": "sha256:" + "a" * 64},
        {"text_digest": "sha256:" + "b" * 64},
        {"token_count": 0},
        {"source_ref": "r" * 128},
    ],
)
def test_shape_valid_references_pass(extra: dict[str, Any]) -> None:
    guard_payload(extra, known=frozenset())


@pytest.mark.parametrize(
    "key",
    [
        "t\u0435xt",  # Cyrillic 'ie' — normalise_key would strip it to 'txt'
        "entry_\u0442ext",
        "\uff54\uff45\uff58\uff54",  # full-width 'text'
        "caf\u00e9",
    ],
)
def test_non_ascii_extra_keys_are_rejected(key: str) -> None:
    with pytest.raises(ContentCaptureError, match="ASCII"):
        guard_payload({key: "x"}, known=frozenset())


def test_non_string_extra_key_is_rejected() -> None:
    with pytest.raises(ContentCaptureError, match="not a string"):
        guard_payload({1: "x"}, known=frozenset())  # type: ignore[dict-item]


def test_secret_error_names_rule_not_value() -> None:
    secret = "sk-" + "Z" * 40
    with pytest.raises(ContentCaptureError) as exc:
        guard_payload({"owner": secret}, known=frozenset())
    assert secret not in str(exc.value)


def test_block_level_extras_are_guarded_too() -> None:
    rows = _rows()
    data: dict[str, Any] = {"store_id": STORE, "accesses": [r.model_dump() for r in rows]}
    data.update({f"n{i}": "x" for i in range(access_mod.MAX_EXTRA_KEYS + 1)})
    with pytest.raises(ContentCaptureError):
        AccessLedgerBlock.model_validate(data)


def test_value_digest_must_be_a_digest_not_a_value() -> None:
    with pytest.raises(InvalidDigestError):
        record_access(
            [],
            agent="a",
            store_id=STORE,
            namespace="n",
            entry_id="e",
            access="read",
            allowed_scope="*",
            value_digest="the actual entry text",
        )


def test_digest_with_trailing_newline_is_rejected() -> None:
    with pytest.raises(InvalidDigestError):
        record_access(
            [],
            agent="a",
            store_id=STORE,
            namespace="n",
            entry_id="e",
            access="read",
            allowed_scope="*",
            value_digest=digest_value("v") + "\n",
        )


@pytest.mark.parametrize("at", ["", "x" * 65, 5])
def test_bad_timestamp_rejected(at: object) -> None:
    with pytest.raises(ValidationError):
        record_access(
            [],
            agent="a",
            store_id=STORE,
            namespace="n",
            entry_id="e",
            access="read",
            allowed_scope="*",
            at=at,  # type: ignore[arg-type]
        )


# ── Chain ─────────────────────────────────────────────────────────────────


def test_chain_links_and_verifies() -> None:
    rows = _rows(3)
    assert rows[0].prev_record_hash is None
    assert verify_access_chain(rows).ok
    assert verify_access_chain([]).ok


def test_reordered_rows_break_the_chain() -> None:
    rows = _rows(2)
    result = verify_access_chain([rows[0], rows[2], rows[1]])
    assert not result.ok and result.broken_at == 1


def test_edited_row_breaks_at_successor() -> None:
    rows = _rows(2)
    edited = rows[1].model_copy(update={"agent": "someone-else"})
    result = verify_access_chain([rows[0], edited, rows[2]])
    assert not result.ok and result.broken_at == 2


def test_record_access_does_not_mutate_input() -> None:
    rows = _rows()
    before = list(rows)
    _rows_extend(rows)
    assert rows == before


# ── Block verification ────────────────────────────────────────────────────


def test_block_verifies_and_detects_truncation() -> None:
    rows = _rows(2)
    block = build_access_block(STORE, rows)
    assert block is not None and block.verified is not None
    assert block.verified.chain_ok and block.verified.scope_flags_ok
    truncated = block.model_copy(update={"accesses": rows[:2]})
    verdict = verify_access_block(truncated)
    assert not verdict.chain_ok and "head" in (verdict.reason or "")


def test_block_detects_foreign_store_row() -> None:
    rows = record_access(
        [],
        agent="a",
        store_id="other",
        namespace="n",
        entry_id="e",
        access="read",
        allowed_scope="*",
    )
    block = AccessLedgerBlock(store_id=STORE, ledger_ref=access_head(rows), accesses=rows)
    verdict = verify_access_block(block)
    assert not verdict.chain_ok and verdict.broken_at == 0


def test_empty_access_list_builds_no_block() -> None:
    assert build_access_block(STORE, []) is None


# ── Facet attachment (I-3) ────────────────────────────────────────────────


def test_none_block_returns_capsule_unchanged() -> None:
    capsule = _load("store-less-capsule.json")
    assert attach_access(capsule, None) is capsule
    assert access_block_from_capsule(capsule) is None


def test_attach_creates_minimal_facet_and_validates_against_schema() -> None:
    capsule = _load("store-less-capsule.json")
    out = attach_access(capsule, build_access_block(STORE, _rows()))
    assert "facets" not in capsule  # input untouched
    facet = out["facets"]["memstore_mutation"]
    assert facet["store_id"] == STORE and "access" in facet
    jsonschema.validate(out, SCHEMA)
    block = access_block_from_capsule(out)
    assert block is not None and verify_access_block(block).chain_ok


def test_attach_preserves_the_mutation_facet() -> None:
    capsule = attach_facet({"run_id": "r"}, build_facet(STORE))
    out = attach_access(capsule, build_access_block(STORE, _rows()))
    assert out["facets"]["memstore_mutation"]["records_in_this_run"] == []
    assert "access" in out["facets"]["memstore_mutation"]


def test_attach_refuses_another_stores_facet() -> None:
    capsule = attach_facet({"run_id": "r"}, build_facet("some-other-store"))
    with pytest.raises(StoreMismatchError):
        attach_access(capsule, build_access_block(STORE, _rows()))


# ── C2 composition ────────────────────────────────────────────────────────


def _event(**kw: Any) -> MemoryOperationEvent:
    base = {
        "run_id": "run_C",
        "capsule_id": "cap",
        "timestamp_utc": "2026-07-14T08:00:00Z",
        "operation": "read",
        "memory_key": "pb-1",
        "agent_id": "support-agent",
        "origin_run_id": "run_A",
        "value": "SECRET CONTENT",
    }
    base.update(kw)
    return MemoryOperationEvent(**base)  # type: ignore[arg-type]


def test_memory_event_maps_without_its_value() -> None:
    kwargs = access_kwargs_from_memory_event(_event())
    assert "SECRET CONTENT" not in json.dumps(kwargs)
    rows = record_access(
        [], store_id=STORE, namespace="playbooks", allowed_scope="playbooks/*", **kwargs
    )
    assert rows[0].claimed_origin_run == "run_A"
    assert rows[0].access == "read" and rows[0].contained


@pytest.mark.parametrize("op", ["write", "update", "delete"])
def test_memory_event_writes_map_to_write(op: str) -> None:
    kwargs = access_kwargs_from_memory_event(_event(operation=op))
    assert kwargs["access"] == "write" and "claimed_origin_run" not in kwargs


def test_memory_event_without_agent_does_not_invent_one() -> None:
    kwargs = access_kwargs_from_memory_event(_event(agent_id=None))
    assert "agent" not in kwargs


# ── Golden fixtures ───────────────────────────────────────────────────────


def test_valid_access_fixture_verifies() -> None:
    block = AccessLedgerBlock.model_validate(_load("valid-access-ledger.json")["block"])
    verdict = verify_access_block(block)
    assert verdict.chain_ok and verdict.scope_flags_ok
    assert [r.contained for r in block.accesses] == [True, False, True]


def test_forged_contained_fixture_is_caught() -> None:
    block = AccessLedgerBlock.model_validate(_load("invalid-access-forged-contained.json")["block"])
    verdict = verify_access_block(block)
    assert verdict.chain_ok  # the forger re-chained …
    assert not verdict.scope_flags_ok  # … but the flag still gives it away


def test_broken_chain_fixture_is_caught() -> None:
    block = AccessLedgerBlock.model_validate(_load("invalid-access-broken-chain.json")["block"])
    assert not verify_access_block(block).chain_ok


def test_content_smuggled_fixture_is_rejected() -> None:
    with pytest.raises(ContentCaptureError):
        AccessLedgerBlock.model_validate(_load("invalid-access-content-smuggled.json")["block"])


# ── P1 hardening carried in this slice ────────────────────────────────────


def test_p1_digest_rejects_trailing_newline() -> None:
    from novafabric.memstore import MutationActor, append_mutation

    with pytest.raises(InvalidDigestError):
        append_mutation(
            [],
            store_id=STORE,
            namespace="n",
            entry_id="e",
            op="create",
            by=MutationActor(agent="a"),
            at="t",
            value_digest=digest_value("v") + "\n",
        )


def test_p1_timestamp_is_capped() -> None:
    from novafabric.memstore import MutationActor, append_mutation

    with pytest.raises(ValidationError):
        append_mutation(
            [],
            store_id=STORE,
            namespace="n",
            entry_id="e",
            op="create",
            by=MutationActor(agent="a"),
            at="x" * 65,
            value_digest=digest_value("v"),
        )
