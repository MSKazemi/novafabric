"""Portable A2A Agent Card facet — ADR-0149 D1 / NF-171.

The property that carries the most weight here is a negative one: `signature_ok`
must never be `True` because a card merely *has* a signature block. A2A cards are
JWS-signed, no JWS verifier is wired, and a fabricated verdict in evidence is worse
than an honest gap — spec I-3 says a partial object degrades to a recorded gap.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novafabric.a2a.card import (
    UNSIGNED,
    UNVERIFIED,
    A2ACardError,
    attach_facet,
    build_facet,
    export_filename,
    facet_from_capsule,
    is_signed,
    verify_facet,
    write_portable_export,
)
from novafabric.a2a.messages import card_fingerprint

CARD = {
    "name": "planner",
    "provider": {"organization": "acme"},
    "url": "https://acme.example/a2a",
    "skills": [{"id": "plan", "name": "Plan tasks"}],
    "capabilities": {"streaming": True},
    "securitySchemes": {"oauth2": {"type": "oauth2"}},
    "signature": {"alg": "EdDSA", "sig": "deadbeef"},
}


# ── the MUST fields ──────────────────────────────────────────────────────────


def test_the_facet_carries_every_required_field() -> None:
    facet = build_facet(CARD, well_known_url="https://acme.example/.well-known/agent.json")
    payload = facet.model_dump(exclude_none=True)

    for field in ("a2a_version", "well_known_url", "card", "card_fingerprint",
                  "signed", "signature_status"):
        assert field in payload, f"spec §5 requires {field}"
    assert payload["card"] == CARD, "the full card must travel, not a summary"


def test_card_identity_is_the_shared_fingerprint() -> None:
    """NF-089, NF-101 and NF-171 must agree on which card an agent presented."""
    facet = build_facet(CARD)
    assert facet.card_fingerprint == card_fingerprint(CARD)


def test_the_fingerprint_ignores_the_signature_block() -> None:
    """A re-signed card is the same card; that is the existing helper's contract."""
    resigned = dict(CARD, signature={"alg": "EdDSA", "sig": "0000"})
    assert build_facet(resigned).card_fingerprint == build_facet(CARD).card_fingerprint


# ── signature_ok is never fabricated ─────────────────────────────────────────


def test_a_signature_block_alone_does_not_produce_a_verdict() -> None:
    """The whole point: `signed` is structural, `signature_ok` needs a verifier."""
    facet = build_facet(CARD)

    assert facet.signed is True
    assert facet.signature_ok is None
    assert facet.signature_status == UNVERIFIED


def test_an_unsigned_card_says_so() -> None:
    unsigned = {k: v for k, v in CARD.items() if k != "signature"}
    facet = build_facet(unsigned)

    assert facet.signed is False
    assert facet.signature_ok is None
    assert facet.signature_status == UNSIGNED


def test_a_supplied_verifier_produces_a_real_verdict() -> None:
    ok = build_facet(CARD, verifier=lambda c: True)
    bad = build_facet(CARD, verifier=lambda c: False)

    assert ok.signature_ok is True and ok.signature_status == "verified"
    assert bad.signature_ok is False
    assert "failed" in bad.signature_status


def test_a_broken_verifier_is_recorded_not_raised() -> None:
    """I-3: a broken verifier must not block a capture."""
    def boom(card: object) -> bool:
        raise RuntimeError("keyserver down")

    facet = build_facet(CARD, verifier=boom)

    assert facet.signature_ok is False
    assert "keyserver down" in facet.signature_status


def test_is_signed_is_structural_only() -> None:
    assert is_signed(CARD) is True
    assert is_signed({"name": "x"}) is False
    assert is_signed({"name": "x", "signature": {}}) is False, "empty block is not signed"


# ── partial cards degrade, they do not raise ─────────────────────────────────


def test_a_partial_card_records_what_is_missing(recwarn: object) -> None:
    facet = build_facet({"name": "planner"})

    assert facet.card_fingerprint.startswith("sha256:")
    assert "skills" in facet.missing_fields
    assert "securitySchemes" in facet.missing_fields


def test_an_empty_card_is_refused_rather_than_stored_hollow() -> None:
    with pytest.raises(A2ACardError, match="must not be empty"):
        build_facet({})


# ── offline re-verification ──────────────────────────────────────────────────


def test_verification_passes_on_an_untouched_card() -> None:
    result = verify_facet(build_facet(CARD))
    assert result.fingerprint_matches is True


def test_verification_detects_an_altered_card() -> None:
    facet = build_facet(CARD)
    tampered = facet.model_copy(update={"card": dict(CARD, name="impostor")})

    result = verify_facet(tampered)

    assert result.fingerprint_matches is False
    assert result.recorded_fingerprint == facet.card_fingerprint
    assert result.observed_fingerprint != facet.card_fingerprint


def test_verification_never_rewrites_the_recorded_fingerprint() -> None:
    facet = build_facet(CARD)
    tampered = facet.model_copy(update={"card": dict(CARD, name="impostor")})
    before = tampered.model_dump()

    verify_facet(tampered)

    assert tampered.model_dump() == before


# ── portable export ──────────────────────────────────────────────────────────


def test_the_export_is_the_card_verbatim(tmp_path: Path) -> None:
    """An A2A-aware tool must read it without knowing NovaFabric's schema."""
    facet = build_facet(CARD)
    path = write_portable_export(facet, tmp_path / "outputs")

    assert json.loads(path.read_text()) == CARD


def test_the_export_is_content_addressed() -> None:
    facet = build_facet(CARD)
    name = export_filename(facet.card_fingerprint)

    assert name.startswith("a2a-card-")
    assert name.endswith(".json")
    assert facet.card_fingerprint.split(":")[1][:8] in name


# ── additive and fail-open ───────────────────────────────────────────────────


def test_no_facet_leaves_the_capsule_byte_identical() -> None:
    capsule = {"run_id": "r", "facets": {"other": {"x": 1}}}
    original = json.dumps(capsule, sort_keys=True)

    assert attach_facet(capsule, None) == capsule
    assert json.dumps(capsule, sort_keys=True) == original


def test_attaching_does_not_mutate_or_drop_siblings() -> None:
    capsule: dict = {"run_id": "r", "facets": {"other": {"x": 1}}}
    out = attach_facet(capsule, build_facet(CARD))

    assert "a2a_card" not in capsule["facets"], "input was mutated"
    assert out["facets"]["other"] == {"x": 1}
    assert out["facets"]["a2a_card"]["card"] == CARD


def test_round_trip_through_a_capsule() -> None:
    facet = build_facet(CARD)
    assert facet_from_capsule(attach_facet({"run_id": "r"}, facet)) == facet


def test_a_capsule_without_the_facet_reads_as_none() -> None:
    assert facet_from_capsule({"run_id": "r"}) is None
    assert facet_from_capsule({"run_id": "r", "facets": {}}) is None
    assert facet_from_capsule({"facets": "not-a-dict"}) is None


def test_an_invalid_facet_is_reported_not_ignored() -> None:
    with pytest.raises(A2ACardError, match="invalid a2a_card facet"):
        facet_from_capsule({"facets": {"a2a_card": {"card": {}}}})
