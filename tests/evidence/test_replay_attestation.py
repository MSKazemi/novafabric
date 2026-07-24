"""ADR-0094 (B) — ReplayAttestation: determinism classifier + signed certificate.

The certificate is an additive superset of the ReperformanceAttestation; it adds
``determinism_class``, the ``pinned`` block, ``bounded_equivalence``,
``input_authorization`` and a ``ledger_ref`` back-link. The classifier returns
each of the three classes; ``attest_replay`` produces a verifiable DSSE
envelope. The model validates against ``schemas/replay-attestation.schema.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from novafabric.capsule.ulid_util import new_ulid
from novafabric.evidence.intoto import dsse_verify
from novafabric.evidence.replay_attestation import (
    REPLAY_ATTESTATION_PREDICATE_TYPE,
    BoundedEquivalence,
    InputAuthorization,
    LedgerRef,
    PinnedBlock,
    PinnedEnv,
    PinnedModel,
    ReplayAttestation,
    attest_replay,
    classify_determinism,
)
from novafabric.evidence.signing import LocalSigner, generate_keypair, verify_with_pem

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "schemas"
    / "replay-attestation.schema.json"
)


@pytest.fixture(scope="module")
def schema_validator() -> Draft202012Validator:
    schema = json.loads(_SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.fixture()
def signer(tmp_path: Path) -> LocalSigner:
    priv, _ = generate_keypair(tmp_path / "keys")
    return LocalSigner(priv)


def _fully_pinned() -> PinnedBlock:
    return PinnedBlock(
        model=PinnedModel(
            request_model="llama-3.1-8b-instruct",
            response_model="llama-3.1-8b-instruct",
            model_digest="sha256:" + "2c" * 32,
            seed=42,
            temperature=0.0,
            top_p=1.0,
        ),
        env=PinnedEnv(
            lock_mode="deterministic",
            container_image_digest="sha256:" + "fc" * 32,
            python_lock_file_hash="sha256:" + "e3" * 32,
            inference_deterministic=True,
        ),
    )


def _underpinned() -> PinnedBlock:
    pinned = _fully_pinned()
    pinned.model.seed = None  # missing seed precludes BIT_EXACT
    return pinned


# --- classifier --------------------------------------------------------------


def test_classify_bit_exact_when_digests_equal_and_fully_pinned() -> None:
    digest = "abc123"
    cls = classify_determinism(digest, digest, _fully_pinned())
    assert cls == "BIT_EXACT"


def test_classify_non_deterministic_when_missing_seed() -> None:
    digest = "abc123"
    cls = classify_determinism(digest, digest, _underpinned())
    assert cls == "NON_DETERMINISTIC"


def test_classify_non_deterministic_on_digest_mismatch_without_metric() -> None:
    cls = classify_determinism("aaa", "bbb", _fully_pinned())
    assert cls == "NON_DETERMINISTIC"


def test_classify_bounded_equivalent_within_threshold() -> None:
    cls = classify_determinism(
        "aaa",
        "bbb",
        _fully_pinned(),
        bounded_metric="semantic-cosine",
        distance=0.04,
        threshold=0.10,
    )
    assert cls == "BOUNDED_EQUIVALENT"


def test_classify_non_deterministic_over_threshold() -> None:
    cls = classify_determinism(
        "aaa",
        "bbb",
        _fully_pinned(),
        bounded_metric="semantic-cosine",
        distance=0.5,
        threshold=0.10,
    )
    assert cls == "NON_DETERMINISTIC"


def test_classify_missing_model_digest_precludes_bit_exact() -> None:
    pinned = _fully_pinned()
    pinned.model.model_digest = None
    cls = classify_determinism("x", "x", pinned)
    assert cls == "NON_DETERMINISTIC"


def test_classify_higher_is_closer_similarity_within_threshold() -> None:
    cls = classify_determinism(
        "aaa",
        "bbb",
        _fully_pinned(),
        bounded_metric="cosine-sim",
        distance=0.95,
        threshold=0.90,
        higher_is_closer=True,
    )
    assert cls == "BOUNDED_EQUIVALENT"


def test_classify_best_effort_lock_precludes_bit_exact() -> None:
    pinned = _fully_pinned()
    pinned.env.lock_mode = "best-effort"
    cls = classify_determinism("x", "x", pinned)
    assert cls == "NON_DETERMINISTIC"


# --- model / schema ----------------------------------------------------------


def _attestation(determinism_class: str = "BIT_EXACT", **extra) -> ReplayAttestation:
    pinned = _underpinned() if determinism_class == "NON_DETERMINISTIC" else _fully_pinned()
    kwargs = {
        "run_id": new_ulid(),
        "capsule_hash": "sha256:" + "9d" * 32,
        "replayed_at": "2026-06-19T11:00:00Z",
        "replay_mode": "exact",
        "outcome_digest": "f1d2d2f924e986ac86fdf7b36c94bcdf32beec15",
        "match": "exact",
        "determinism_class": determinism_class,
        "pinned": pinned,
    }
    kwargs.update(extra)
    return ReplayAttestation(**kwargs)


def test_bit_exact_attestation_validates_against_schema(
    schema_validator: Draft202012Validator,
) -> None:
    att = _attestation(
        "BIT_EXACT",
        ledger_ref=LedgerRef(checkpoint_id=new_ulid(), capsule_id=new_ulid()),
        input_authorization=InputAuthorization(
            inputs_digest="sha256:" + "0b" * 32, authorized_by="alice@example.com"
        ),
    )
    schema_validator.validate(att.to_record())


def test_bounded_equivalent_attestation_validates(
    schema_validator: Draft202012Validator,
) -> None:
    att = _attestation(
        "BOUNDED_EQUIVALENT",
        match="semantic-match",
        replay_mode="semantic",
        bounded_equivalence=BoundedEquivalence(
            metric="semantic-cosine", distance=0.04, threshold=0.10
        ),
    )
    schema_validator.validate(att.to_record())


def test_ledger_ref_links_checkpoint(schema_validator: Draft202012Validator) -> None:
    cid = new_ulid()
    att = _attestation("BIT_EXACT", ledger_ref=LedgerRef(checkpoint_id=cid))
    assert att.ledger_ref is not None
    assert att.ledger_ref.checkpoint_id == cid
    schema_validator.validate(att.to_record())


# --- signed certificate ------------------------------------------------------


def test_attest_replay_produces_verifiable_envelope(
    signer: LocalSigner, schema_validator: Draft202012Validator
) -> None:
    att = _attestation("BIT_EXACT")
    schema_validator.validate(att.to_record())
    envelope = attest_replay(att, signer)

    def verify_fn(pae: bytes, sig: bytes) -> bool:
        return verify_with_pem(signer.public_pem, pae, sig)

    statement = dsse_verify(envelope, verify_fn)
    assert statement["predicateType"] == REPLAY_ATTESTATION_PREDICATE_TYPE
    assert statement["predicate"]["determinism_class"] == "BIT_EXACT"


def test_attest_replay_tamper_rejected(signer: LocalSigner) -> None:
    import base64

    att = _attestation("BIT_EXACT")
    envelope = attest_replay(att, signer)
    tampered = json.loads(base64.b64decode(envelope["payload"]).decode())
    tampered["predicate"]["determinism_class"] = "NON_DETERMINISTIC"
    envelope["payload"] = base64.b64encode(
        json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode()
    ).decode()

    def verify_fn(pae: bytes, sig: bytes) -> bool:
        return verify_with_pem(signer.public_pem, pae, sig)

    with pytest.raises(Exception):
        dsse_verify(envelope, verify_fn)


def test_attestation_from_reperformance_carries_base_fields() -> None:
    """The ReplayAttestation is a superset — base fields are carried unchanged."""
    from novafabric.evidence.reperformance import ReperformanceAttestation
    from novafabric.evidence.replay_attestation import from_reperformance

    base = ReperformanceAttestation(
        run_id="run-x",
        capsule_hash="sha256:" + "ab" * 32,
        replayed_at="2026-06-19T11:00:00Z",
        replay_mode="exact",
        outcome_digest="dig",
        match="exact",
    )
    att = from_reperformance(
        base,
        determinism_class="NON_DETERMINISTIC",
        pinned=_underpinned(),
    )
    assert att.run_id == "run-x"
    assert att.outcome_digest == "dig"
    assert att.match == "exact"
    assert att.determinism_class == "NON_DETERMINISTIC"
