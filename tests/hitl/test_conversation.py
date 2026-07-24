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

"""ADR-0150 P1 — conversation-thread provenance (NF-181).

Tests are organised by the ADR's invariants, because those are what a reviewer
needs to be convinced of: I-1 additive-first, I-2 pseudonymous + digest-only,
I-3 fail-open, I-4 record-only.

The golden fixtures the ADR names — a text-only capsule that stays valid and a
threaded capsule that validates — are checked against the *real*
``run-capsule.schema.json``, not a hand-written dict. Five earlier facet slices
shipped code the schema rejected precisely because their tests never touched
the schema (ADR-0196 D4).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from novafabric.hitl import (
    ConversationError,
    ConversationFacet,
    DuplicateTurnError,
    IdentityRefError,
    Turn,
    TurnContentError,
    TurnTimeError,
    attach_facet,
    broken_parent_refs,
    build_facet,
    dangling_turn_refs,
    digest_turn,
    facet_from_capsule,
    resolve_turn,
    turn,
    verify_turn_binding,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "run-capsule.schema.json"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "conversation"
TEXT_ONLY_CAPSULE = FIXTURES / "valid-text-only-capsule.json"
THREADED_CAPSULE = FIXTURES / "threaded-capsule.json"

HUMAN = "human:did:example:alice"
AGENT = "agent:spiffe://acme.example/ns/agents/sa/triager"
UTTERANCE = "approve the rollback, the error budget is spent"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


def _turn(**kw: Any) -> Turn:
    base: dict[str, Any] = {
        "turn_id": "t0",
        "author": HUMAN,
        "role": "human",
        "content_digest": digest_turn(UTTERANCE),
        "at": "2026-07-15T10:00:00Z",
    }
    base.update(kw)
    return Turn(**base)


# ── I-2: digest-only turns ────────────────────────────────────────────────


def test_digest_is_sha256_of_content_with_algorithm_prefix() -> None:
    expected = hashlib.sha256(UTTERANCE.encode()).hexdigest()
    assert digest_turn(UTTERANCE) == f"sha256:{expected}"


def test_digest_accepts_bytes_and_str_identically() -> None:
    assert digest_turn(UTTERANCE) == digest_turn(UTTERANCE.encode())


def test_turn_content_never_reaches_the_facet() -> None:
    """The single fact this facet exists to keep true.

    `turn(content=...)` is the only path by which turn text may touch this
    module, and the text must not survive into anything serialised.
    """
    facet = build_facet([turn("t0", HUMAN, "human", content=UTTERANCE, at="2026-07-15T10:00:00Z")])
    dumped = facet.model_dump_json()
    assert UTTERANCE not in dumped
    assert digest_turn(UTTERANCE) in dumped


def test_turn_text_passed_as_a_digest_is_rejected() -> None:
    with pytest.raises(TurnContentError):
        _turn(content_digest=UTTERANCE)


def test_rejected_digest_error_does_not_echo_the_content() -> None:
    """An error message lands in a log; the content must not go with it."""
    with pytest.raises(TurnContentError) as excinfo:
        _turn(content_digest=UTTERANCE)
    assert UTTERANCE not in str(excinfo.value)


def test_raw_bytes_are_refused_rather_than_hashed_for_the_caller() -> None:
    """Hashing bytes here would make it effortless to hand this module a
    transcript and have it quietly do the right-looking thing."""
    with pytest.raises(TurnContentError):
        _turn(content_digest=UTTERANCE.encode())


@pytest.mark.parametrize(
    "bad",
    [
        "sha256:" + "a" * 63,  # truncated
        "sha256:" + "A" * 64,  # upper-case hex
        "md5:" + "a" * 32,  # wrong algorithm
        "a" * 64,  # no algorithm prefix
    ],
)
def test_malformed_digests_fail_loudly_at_capture_time(bad: str) -> None:
    with pytest.raises(TurnContentError):
        _turn(content_digest=bad)


def test_turn_requires_exactly_one_of_content_or_digest() -> None:
    with pytest.raises(TurnContentError):
        turn("t0", HUMAN, "human", at="2026-07-15T10:00:00Z")
    with pytest.raises(TurnContentError):
        turn(
            "t0",
            HUMAN,
            "human",
            content=UTTERANCE,
            content_digest=digest_turn(UTTERANCE),
            at="2026-07-15T10:00:00Z",
        )


def test_binding_verifies_against_the_rendered_turn() -> None:
    item = _turn()
    assert verify_turn_binding(item, UTTERANCE) is True
    assert verify_turn_binding(item, UTTERANCE + " ") is False


# ── I-2: pseudonymous identities ──────────────────────────────────────────


@pytest.mark.parametrize(
    "author",
    [
        "human:did:example:alice",
        "human:fp:9f2c4a1b7e0d5638",
        "agent:spiffe://acme.example/ns/agents/sa/triager",
    ],
)
def test_spec_identity_ref_forms_are_accepted(author: str) -> None:
    role = "human" if author.startswith("human:") else "agent"
    assert _turn(author=author, role=role).author == author


@pytest.mark.parametrize(
    "author",
    [
        "Alice Smith",  # a raw name
        "alice",  # unprefixed opaque id
        "did:example:alice",  # a DID with no party scheme
        "",
    ],
)
def test_unprefixed_identities_are_rejected(author: str) -> None:
    with pytest.raises(IdentityRefError):
        _turn(author=author)


def test_email_shaped_author_is_rejected_by_name() -> None:
    """`human:alice@example.com` is well-formed by shape and is still raw PII."""
    with pytest.raises(IdentityRefError, match="email"):
        _turn(author="human:alice@example.com")


@pytest.mark.parametrize("author", [b"human:did:x", 42, None])
def test_non_text_author_is_rejected_with_a_named_error(author: object) -> None:
    """Named rather than folded into a ValidationError: a caller handling I-2
    violations needs to catch this by type."""
    with pytest.raises(IdentityRefError):
        _turn(author=author)


@pytest.mark.parametrize("digest", [42, None, {"sha256": "x"}])
def test_non_text_content_digest_is_rejected_with_a_named_error(
    digest: object,
) -> None:
    with pytest.raises(TurnContentError):
        _turn(content_digest=digest)


def test_author_longer_than_a_reference_is_rejected() -> None:
    with pytest.raises(IdentityRefError):
        _turn(author="human:did:example:" + "x" * 600)


def test_role_and_author_scheme_must_agree() -> None:
    """A human role with an agent author makes 'who said this' unreadable —
    and an auditor counting human turns would silently count wrong."""
    with pytest.raises(IdentityRefError):
        _turn(author=AGENT, role="human")
    with pytest.raises(IdentityRefError):
        _turn(author=HUMAN, role="agent")


def test_no_raw_pii_appears_in_a_serialised_thread() -> None:
    facet = build_facet(
        [
            _turn(turn_id="t0"),
            _turn(turn_id="t1", author=AGENT, role="agent", at="2026-07-15T10:00:03Z"),
        ]
    )
    dumped = facet.model_dump_json()
    assert "@" not in dumped
    assert "Alice" not in dumped


# ── I-1: additive-first ───────────────────────────────────────────────────


def test_capsule_without_conversation_material_is_untouched() -> None:
    """Byte-identical to a capsule captured before this feature existed."""
    capsule = {"run_id": "r", "facets": {"existing": {"a": 1}}}
    assert attach_facet(capsule, build_facet([])) == capsule


def test_session_ref_alone_is_not_material() -> None:
    """A facet with a session back-reference and no turns adds a block, a
    schema version and a seal surface while answering nothing."""
    facet = build_facet([], session_ref=digest_turn("session-root"))
    assert facet.has_material is False
    assert attach_facet({"run_id": "r"}, facet) == {"run_id": "r"}


def test_attach_does_not_mutate_the_input_capsule() -> None:
    capsule: dict[str, Any] = {"run_id": "r"}
    attach_facet(capsule, build_facet([_turn()]))
    assert capsule == {"run_id": "r"}


def test_attach_preserves_sibling_facets() -> None:
    capsule = {"run_id": "r", "facets": {"existing": {"a": 1}}}
    out = attach_facet(capsule, build_facet([_turn()]))
    assert out["facets"]["existing"] == {"a": 1}
    assert "conversation" in out["facets"]


def test_facet_carries_a_schema_version() -> None:
    out = attach_facet({"run_id": "r"}, build_facet([_turn()]))
    assert out["facets"]["conversation"]["schema_version"]


def test_facet_round_trips_through_a_capsule() -> None:
    facet = build_facet([_turn()], session_ref=digest_turn("session-root"))
    out = attach_facet({"run_id": "r"}, facet)
    back = facet_from_capsule(out)
    assert back is not None
    assert [t.turn_id for t in back.turns] == ["t0"]
    assert back.session_ref == facet.session_ref


def test_reading_a_capsule_without_the_facet_is_not_an_error() -> None:
    assert facet_from_capsule({"run_id": "r"}) is None
    assert facet_from_capsule({"run_id": "r", "facets": {}}) is None
    assert facet_from_capsule({"run_id": "r", "facets": {"safety": {}}}) is None


# ── Threading and ordering ────────────────────────────────────────────────


def test_turns_are_ordered_chronologically() -> None:
    facet = build_facet(
        [
            _turn(turn_id="late", at="2026-07-15T10:05:00Z"),
            _turn(turn_id="early", at="2026-07-15T10:00:00Z"),
        ]
    )
    assert [t.turn_id for t in facet.turns] == ["early", "late"]


def test_ordering_is_timezone_aware_not_lexical() -> None:
    """`10:00:00+02:00` precedes `09:00:00Z`; string sorting says otherwise."""
    facet = build_facet(
        [
            _turn(turn_id="second", at="2026-07-15T09:00:00Z"),
            _turn(turn_id="first", at="2026-07-15T10:00:00+02:00"),
        ]
    )
    assert [t.turn_id for t in facet.turns] == ["first", "second"]


def test_equal_timestamps_keep_the_callers_causal_order() -> None:
    """At second granularity two turns collide; the order the capture site
    observed them in is better evidence than any tie-break invented here."""
    facet = build_facet(
        [
            _turn(turn_id="t10", at="2026-07-15T10:00:00Z"),
            _turn(turn_id="t2", at="2026-07-15T10:00:00Z"),
        ]
    )
    assert [t.turn_id for t in facet.turns] == ["t10", "t2"]


def test_unparseable_timestamp_is_refused() -> None:
    with pytest.raises(TurnTimeError):
        _turn(at="last tuesday")


def test_naive_timestamp_is_read_as_utc_rather_than_refused() -> None:
    """Rejecting it would make a whole thread unrecordable (I-3) over a
    formatting detail, and mixing naive with aware breaks ordering outright."""
    facet = build_facet(
        [
            _turn(turn_id="second", at="2026-07-15T10:00:01"),
            _turn(turn_id="first", at="2026-07-15T10:00:00Z"),
        ]
    )
    assert [t.turn_id for t in facet.turns] == ["first", "second"]


def test_duplicate_turn_ids_are_refused() -> None:
    """Fail-open covers absent material; a duplicate id makes every later
    facet's turn_ref resolve ambiguously to the wrong moment."""
    with pytest.raises(DuplicateTurnError):
        build_facet([_turn(turn_id="t0"), _turn(turn_id="t0", at="2026-07-15T10:00:09Z")])


def test_empty_turn_id_is_refused() -> None:
    with pytest.raises(ConversationError):
        _turn(turn_id="   ")


def test_named_errors_survive_pydantic_validation() -> None:
    """Every error type here subclasses Exception, not ValueError.

    Pydantic v2 folds a validator's ValueError into a ValidationError, which
    would destroy the named type and leave a caller unable to tell an I-2
    identity violation from a malformed digest.
    """
    for exc_type in (ConversationError, IdentityRefError, TurnContentError, TurnTimeError):
        assert not issubclass(exc_type, ValueError)


# ── turn_ref resolution (spec §3.5) ───────────────────────────────────────


def test_turn_ref_resolves_to_its_turn() -> None:
    facet = build_facet([_turn(turn_id="t0"), _turn(turn_id="t7", at="2026-07-15T10:07:00Z")])
    resolved = resolve_turn(facet, "t7")
    assert resolved is not None
    assert resolved.turn_id == "t7"


def test_unknown_turn_ref_resolves_to_none_rather_than_raising() -> None:
    """The caller decides whether a dangling ref is fatal in its context (I-4)."""
    assert resolve_turn(build_facet([_turn()]), "t99") is None


def test_dangling_turn_refs_are_flagged_in_order_with_duplicates_kept() -> None:
    facet = build_facet([_turn(turn_id="t0")])
    assert dangling_turn_refs(facet, ["t0", "t9", "t8", "t9"]) == ["t9", "t8", "t9"]


def test_thread_root_is_not_a_broken_parent() -> None:
    assert broken_parent_refs(build_facet([_turn(parent_turn_id=None)])) == []


def test_parent_naming_an_absent_turn_is_flagged() -> None:
    facet = build_facet([_turn(turn_id="t1", parent_turn_id="t0")])
    assert broken_parent_refs(facet) == ["t1"]


def test_self_parenting_turn_is_flagged_as_broken() -> None:
    """A one-node cycle would hang any thread walker."""
    facet = build_facet([_turn(turn_id="t0", parent_turn_id="t0")])
    assert broken_parent_refs(facet) == ["t0"]


def test_a_well_formed_thread_has_no_broken_parents() -> None:
    facet = build_facet(
        [
            _turn(turn_id="t0"),
            _turn(
                turn_id="t1",
                author=AGENT,
                role="agent",
                parent_turn_id="t0",
                at="2026-07-15T10:00:03Z",
            ),
        ]
    )
    assert broken_parent_refs(facet) == []


# ── I-4: record-only, absent is not false ─────────────────────────────────


def test_absent_parent_serialises_as_absent_not_null() -> None:
    """A thread root and a turn whose parent was dropped must not serialise
    identically: absent means unknown, never a claim."""
    out = attach_facet({"run_id": "r"}, build_facet([_turn()]))
    assert "parent_turn_id" not in out["facets"]["conversation"]["turns"][0]


def test_module_exposes_no_adjudication_surface() -> None:
    """Record-only is a property of the API, not just of the docs.

    If an approve/adjudicate/decide entry point ever appears here, this fails —
    which is the point: I-4 should be structurally hard to violate.
    """
    import novafabric.hitl as hitl

    forbidden = {"approve", "adjudicate", "decide", "grant", "deny", "certify"}
    assert forbidden.isdisjoint({name.lower() for name in hitl.__all__}), (
        "hitl must not expose an adjudication entry point (ADR-0150 D7)"
    )


def test_facet_accepts_unknown_keys_for_later_slices() -> None:
    """P2's decision-context/override/rationale must be able to extend a turn
    without a schema break — that is what extra='allow' buys."""
    facet = ConversationFacet.model_validate(
        {
            "schema_version": "0.1.0",
            "turns": [
                {
                    "turn_id": "t0",
                    "author": HUMAN,
                    "role": "human",
                    "content_digest": digest_turn(UTTERANCE),
                    "at": "2026-07-15T10:00:00Z",
                    "future_p2_field": {"context_root": "sha256:" + "a" * 64},
                }
            ],
            "future_facet_field": 1,
        }
    )
    assert facet.turns[0].turn_id == "t0"


# ── Golden fixtures against the real schema (ADR-0196 D4) ─────────────────


def test_golden_text_only_capsule_stays_valid(schema: dict[str, Any]) -> None:
    """Fixture 1 from the ADR's P1 bullet: a capsule with no conversation
    material is unaffected by this feature existing."""
    capsule = json.loads(TEXT_ONLY_CAPSULE.read_text())
    assert "facets" not in capsule
    jsonschema.validate(capsule, schema)


def test_golden_threaded_capsule_is_valid(schema: dict[str, Any]) -> None:
    """Fixture 2 from the ADR's P1 bullet: a threaded capsule validates."""
    capsule = json.loads(THREADED_CAPSULE.read_text())
    jsonschema.validate(capsule, schema)


def test_golden_threaded_capsule_parses_back_into_the_model(
    schema: dict[str, Any],
) -> None:
    capsule = json.loads(THREADED_CAPSULE.read_text())
    facet = facet_from_capsule(capsule)
    assert facet is not None
    assert [t.turn_id for t in facet.turns] == ["t0", "t1", "t2"]
    assert [t.role for t in facet.turns] == ["human", "agent", "human"]
    assert broken_parent_refs(facet) == []


def test_builder_output_validates_against_the_real_schema(
    schema: dict[str, Any],
) -> None:
    """The exact regression ADR-0196 D4 exists to prevent.

    Uses the shipped builder rather than a hand-written dict, because a
    hand-written dict is what let the gap through for five earlier facets.
    """
    capsule = json.loads(TEXT_ONLY_CAPSULE.read_text())
    facet = build_facet(
        [
            turn("t0", HUMAN, "human", content=UTTERANCE, at="2026-07-15T10:00:00Z"),
            turn(
                "t1",
                AGENT,
                "agent",
                content="rolling back",
                parent_turn_id="t0",
                at="2026-07-15T10:00:03Z",
            ),
        ],
        session_ref=digest_turn("session-root"),
    )
    jsonschema.validate(attach_facet(capsule, facet), schema)


def test_no_material_attach_is_a_no_op_and_still_validates(
    schema: dict[str, Any],
) -> None:
    capsule = json.loads(TEXT_ONLY_CAPSULE.read_text())
    out = attach_facet(capsule, build_facet([]))
    assert out == capsule
    jsonschema.validate(out, schema)
