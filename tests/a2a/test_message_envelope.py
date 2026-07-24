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

"""ADR-0142 P1 — A2A message-envelope facet (NF-101).

Tests are organised by the ADR's invariants, because those are what a reviewer
needs to be convinced of: I-1 additive-first, I-2 reference-only/no payloads,
I-3 fail-open and absent-is-not-false, I-4 record-not-adjudicate — plus the two
golden fixtures the ADR's P1 bullet names.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from novafabric.a2a import (
    A2AMessageEnvelope,
    CardBindingError,
    DuplicateMessageIdError,
    EmptyMessagePartsError,
    MessagePart,
    attach_facet,
    build_facet,
    card_fingerprint,
    card_status,
    content_hash_for_parts,
    envelope_from_parts,
    verify_card_binding,
    verify_content_binding,
)

REPO_ROOT = Path(__file__).parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "a2a-messages"
CAPSULE_SCHEMA = REPO_ROOT / "schemas" / "run-capsule.schema.json"

SECRET_TEXT = "here is the api key sk-live-do-not-capture-me"

CARD: dict[str, Any] = {
    "protocolVersion": "1.0",
    "name": "planner",
    "skills": [{"id": "plan", "name": "plan"}],
    "signatures": [{"protected": "eyJhbGciOiJFZERTQSJ9", "signature": "c2ln"}],
}


def _parts(text: str = "hello") -> list[MessagePart]:
    return [MessagePart(kind="text", text=text)]


def _envelope(**kw: Any) -> A2AMessageEnvelope:
    base: dict[str, Any] = {
        "msg_id": "m-01",
        "from_agent": "planner",
        "to_agent": "researcher",
        "a2a_role": "ROLE_AGENT",
        "task_id": "t-42",
        "content_hash": content_hash_for_parts(_parts()),
        "sent_at": "2026-07-12T10:00:01.000000Z",
    }
    base.update(kw)
    return A2AMessageEnvelope(**base)


# ── Content binding ───────────────────────────────────────────────────────


def test_content_hash_is_sha256_of_canonical_parts_with_algorithm_prefix() -> None:
    body = [{"kind": "text", "text": "hello"}]
    canonical = json.dumps(
        body, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    expected = hashlib.sha256(canonical).hexdigest()
    assert content_hash_for_parts(_parts()) == f"sha256:{expected}"


def test_omitted_and_explicitly_null_optionals_hash_identically() -> None:
    """A caller that spells out `data=None` must not get a different digest."""
    assert content_hash_for_parts(
        [MessagePart(kind="text", text="hello", data=None)]
    ) == content_hash_for_parts([MessagePart(kind="text", text="hello")])


def test_part_order_changes_the_digest() -> None:
    """Two messages with the same parts in a different order say different things."""
    a = MessagePart(kind="text", text="first")
    b = MessagePart(kind="text", text="second")
    assert content_hash_for_parts([a, b]) != content_hash_for_parts([b, a])


def test_zero_parts_cannot_be_content_bound() -> None:
    """A digest over zero parts binds nothing but would look like evidence.

    A caller whose part extraction silently failed must not walk away with a
    hash that verifies against every other empty message.
    """
    with pytest.raises(EmptyMessagePartsError):
        content_hash_for_parts([])


def test_binding_verifies_against_the_message_parts() -> None:
    env = _envelope()
    assert verify_content_binding(env, _parts()) is True
    assert verify_content_binding(env, _parts("hello ")) is False


def test_unbound_envelope_does_not_verify() -> None:
    assert verify_content_binding(_envelope(content_hash=""), _parts()) is False


def test_verifying_against_zero_parts_is_not_a_pass() -> None:
    assert verify_content_binding(_envelope(), []) is False


# ── Card binding ──────────────────────────────────────────────────────────


def test_card_fingerprint_ignores_the_signature_block() -> None:
    """Re-signing the same claims must not change which card the record names."""
    resigned = {**CARD, "signatures": [{"protected": "eyJhbGciOiJFZERTQSJ9", "signature": "b3RoZXI"}]}
    assert card_fingerprint(CARD) == card_fingerprint(resigned)


def test_card_fingerprint_covers_every_non_signature_claim() -> None:
    drifted = {**CARD, "skills": [{"id": "plan", "name": "plan"}, {"id": "spend", "name": "spend"}]}
    assert card_fingerprint(CARD) != card_fingerprint(drifted)


def test_card_binding_verifies_against_the_sending_card() -> None:
    env = envelope_from_parts(
        msg_id="m-01",
        from_agent="planner",
        to_agent="researcher",
        a2a_role="ROLE_AGENT",
        task_id="t-42",
        sent_at="2026-07-12T10:00:01.000000Z",
        parts=_parts(),
        sender_card=CARD,
        card_verified=True,
    )
    assert verify_card_binding(env, CARD) is True
    assert verify_card_binding(env, {**CARD, "name": "impostor"}) is False


def test_envelope_without_a_fingerprint_does_not_verify_a_card() -> None:
    assert verify_card_binding(_envelope(), CARD) is False


def test_card_verdict_without_a_card_is_rejected() -> None:
    """A verdict with nothing to point at cannot be re-checked by a verifier."""
    with pytest.raises(CardBindingError):
        envelope_from_parts(
            msg_id="m-01",
            from_agent="planner",
            to_agent="researcher",
            a2a_role="ROLE_AGENT",
            task_id="t-42",
            sent_at="2026-07-12T10:00:01.000000Z",
            parts=_parts(),
            card_verified=True,
        )


# ── I-3: fail-open, absent is not false ───────────────────────────────────


def test_unchecked_card_is_unknown_not_unverified() -> None:
    """A missing verdict is "not checked", never a failed verification."""
    env = envelope_from_parts(
        msg_id="m-01",
        from_agent="planner",
        to_agent="researcher",
        a2a_role="ROLE_AGENT",
        task_id="t-42",
        sent_at="2026-07-12T10:00:01.000000Z",
        parts=_parts(),
        sender_card=CARD,
    )
    assert env.card_verified is None
    assert card_status(env) == "unknown"


def test_no_card_at_all_is_unknown() -> None:
    """Fail-open: an unfetchable peer card leaves a gap, not a verdict."""
    assert card_status(_envelope()) == "unknown"


def test_a_recorded_verification_failure_stays_unverified() -> None:
    """`False` means someone checked and it failed — distinct from unknown."""
    env = _envelope(card_fingerprint=card_fingerprint(CARD), card_verified=False)
    assert card_status(env) == "unverified"


def test_a_recorded_verification_success_is_verified() -> None:
    env = _envelope(card_fingerprint=card_fingerprint(CARD), card_verified=True)
    assert card_status(env) == "verified"


def test_verdict_without_a_fingerprint_is_not_reported_as_verified() -> None:
    """A `True` that names no card is unfalsifiable, so it is not a pass."""
    assert card_status(_envelope(card_verified=True)) == "unknown"


# ── I-2: reference-only, no payloads ──────────────────────────────────────


def test_message_text_never_reaches_the_envelope() -> None:
    env = envelope_from_parts(
        msg_id="m-01",
        from_agent="planner",
        to_agent="researcher",
        a2a_role="ROLE_AGENT",
        task_id="t-42",
        sent_at="2026-07-12T10:00:01.000000Z",
        parts=_parts(SECRET_TEXT),
        sender_card=CARD,
        card_verified=True,
    )
    dumped = env.model_dump_json()
    assert SECRET_TEXT not in dumped
    assert content_hash_for_parts(_parts(SECRET_TEXT)) in dumped


def test_structured_data_parts_are_also_only_digested() -> None:
    env = envelope_from_parts(
        msg_id="m-01",
        from_agent="planner",
        to_agent="researcher",
        a2a_role="ROLE_AGENT",
        task_id="t-42",
        sent_at="2026-07-12T10:00:01.000000Z",
        parts=[MessagePart(kind="data", data={"token": SECRET_TEXT})],
    )
    assert SECRET_TEXT not in env.model_dump_json()


def test_blob_ref_cannot_hold_a_body_in_p1() -> None:
    """P1 ships no byte capture, enforced by the model rather than by promise."""
    with pytest.raises(ValueError):
        _envelope(blob_ref="blobs/sha256-aa11")


def test_attached_facet_states_blob_ref_null_explicitly() -> None:
    out = attach_facet({"run_id": "r"}, build_facet([_envelope()]))
    message = out["facets"]["a2a_messages"]["messages"][0]
    assert "blob_ref" in message
    assert message["blob_ref"] is None


# ── I-1: additive-first ───────────────────────────────────────────────────


def test_capsule_without_a2a_material_is_untouched() -> None:
    """Byte-identical to a capsule captured before this feature existed."""
    capsule = {"run_id": "r", "facets": {"existing": {"a": 1}}}
    assert attach_facet(capsule, build_facet([])) == capsule


def test_attach_does_not_mutate_the_input_capsule() -> None:
    capsule: dict[str, Any] = {"run_id": "r"}
    attach_facet(capsule, build_facet([_envelope()]))
    assert capsule == {"run_id": "r"}


def test_attach_preserves_sibling_facets() -> None:
    capsule = {"run_id": "r", "facets": {"existing": {"a": 1}}}
    out = attach_facet(capsule, build_facet([_envelope()]))
    assert out["facets"]["existing"] == {"a": 1}
    assert "a2a_messages" in out["facets"]


def test_facet_carries_schema_and_a2a_versions() -> None:
    out = attach_facet({"run_id": "r"}, build_facet([_envelope()]))
    facet = out["facets"]["a2a_messages"]
    assert facet["schema_version"]
    assert facet["a2a_version"] == "1.0"


# ── Facet assembly ────────────────────────────────────────────────────────


def test_facet_orders_messages_deterministically_by_send_time() -> None:
    facet = build_facet(
        [
            _envelope(msg_id="late", sent_at="2026-07-12T12:00:00.000000Z"),
            _envelope(msg_id="early", sent_at="2026-07-12T10:00:00.000000Z"),
        ]
    )
    assert [m.msg_id for m in facet.messages] == ["early", "late"]


def test_same_instant_messages_are_ordered_by_msg_id_not_left_unstable() -> None:
    """A total order keeps the serialised facet — and any hash over it — stable."""
    at = "2026-07-12T10:00:00.000000Z"
    facet = build_facet([_envelope(msg_id="m-b", sent_at=at), _envelope(msg_id="m-a", sent_at=at)])
    assert [m.msg_id for m in facet.messages] == ["m-a", "m-b"]


def test_duplicate_message_ids_are_rejected() -> None:
    """Dropping one would lose a message; keeping both leaves the record ambiguous."""
    with pytest.raises(DuplicateMessageIdError):
        build_facet([_envelope(msg_id="m-01"), _envelope(msg_id="m-01")])


def test_vclock_is_carried_through_untouched() -> None:
    """P1 records the clock for NF-105; it does not interpret it."""
    env = _envelope(vclock={"planner": 3, "researcher": 1})
    facet = build_facet([env])
    assert facet.messages[0].vclock == {"planner": 3, "researcher": 1}


# ── I-4: record, do not adjudicate ────────────────────────────────────────


def test_module_exposes_no_orchestration_surface() -> None:
    """Record-only is a property of the API, not just of the docs.

    If a send/route/dispatch entry point ever appears here, this fails — which
    is the point: NovaFabric records the wire, it is not a message bus
    (ADR-0142 alternatives §3, non-goals.md).
    """
    import novafabric.a2a as a2a_evidence

    forbidden = {"send", "route", "dispatch", "broker", "orchestrate", "negotiate"}
    assert forbidden.isdisjoint({name.lower() for name in a2a_evidence.__all__})


# ── Golden fixtures (ADR-0142 P1) ─────────────────────────────────────────


def test_single_agent_capsule_stays_schema_valid() -> None:
    """Golden fixture 1: a capsule with no A2A material is unchanged and valid."""
    capsule = json.loads((FIXTURE_DIR / "single-agent-capsule.json").read_text())
    out = attach_facet(capsule, build_facet([]))

    assert out == capsule
    validator = jsonschema.Draft202012Validator(
        json.loads(CAPSULE_SCHEMA.read_text()), format_checker=jsonschema.FormatChecker()
    )
    assert list(validator.iter_errors(out)) == []


def test_two_agent_signed_exchange_round_trips() -> None:
    """Golden fixture 2: rebuilding from the wire reproduces the sealed facet."""
    golden = json.loads((FIXTURE_DIR / "two-agent-exchange.json").read_text())
    cards = golden["cards"]

    envelopes = [
        envelope_from_parts(
            msg_id=m["msg_id"],
            from_agent=m["from_agent"],
            to_agent=m["to_agent"],
            a2a_role=m["a2a_role"],
            task_id=m["task_id"],
            sent_at=m["sent_at"],
            parts=[MessagePart(**p) for p in m["parts"]],
            sender_card=cards[m["from_agent"]],
            card_verified=True,
            vclock=m["vclock"],
            sig=m["sig"],
        )
        for m in golden["wire"]
    ]
    rebuilt = attach_facet({"run_id": "r"}, build_facet(envelopes))
    assert rebuilt["facets"]["a2a_messages"] == golden["expected_facet"]


def test_two_agent_golden_facet_verifies_against_its_wire() -> None:
    """Every sealed envelope re-verifies against the parts and card it names."""
    golden = json.loads((FIXTURE_DIR / "two-agent-exchange.json").read_text())
    by_id = {m["msg_id"]: m for m in golden["wire"]}

    for recorded in golden["expected_facet"]["messages"]:
        env = A2AMessageEnvelope(**recorded)
        wire = by_id[env.msg_id]
        assert verify_content_binding(env, [MessagePart(**p) for p in wire["parts"]])
        assert verify_card_binding(env, golden["cards"][env.from_agent])
        assert card_status(env) == "verified"


def test_two_agent_golden_facet_carries_no_message_text() -> None:
    """The sealed record must not contain a byte of what was said."""
    golden = json.loads((FIXTURE_DIR / "two-agent-exchange.json").read_text())
    sealed = json.dumps(golden["expected_facet"])
    for message in golden["wire"]:
        for part in message["parts"]:
            if part.get("text"):
                assert part["text"] not in sealed
