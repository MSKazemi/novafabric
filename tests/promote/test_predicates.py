"""Tests for promote predicate builders and schema validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novafabric.promote.exceptions import PredicateValidationError
from novafabric.promote.predicates import (
    APPROVAL_PAYLOAD_TYPE,
    PROPOSAL_PAYLOAD_TYPE,
    build_approval_predicate,
    build_policy_predicate,
    build_proposal_predicate,
    sign_promote_envelope,
    validate_predicate,
    verify_promote_envelope,
)

FIXTURE_KEYS = Path(__file__).parent.parent / "fixtures" / "promote" / "keys"


# ---------------------------------------------------------------------------
# Predicate builder tests
# ---------------------------------------------------------------------------


def test_build_proposal_predicate_keys() -> None:
    pred = build_proposal_predicate(
        capsule_id="cap-001",
        capsule_digest_hex="a" * 64,
        target_env="staging",
        justification="This capsule has passed all automated checks and peer review.",
        proposer_subject="proposer",
        policy_version="1",
    )
    assert pred["role"] == "proposer"
    assert pred["capsule_id"] == "cap-001"
    assert pred["capsule_digest"] == {"sha256": "a" * 64}
    assert pred["target_environment"] == "staging"
    assert pred["policy_version"] == "1"
    assert "timestamp" in pred


def test_build_approval_predicate_keys(
    proposer_key_path: Path, proposer_cert_path: Path
) -> None:
    import json as _json

    proposal_pred = build_proposal_predicate(
        capsule_id="cap-001",
        capsule_digest_hex="a" * 64,
        target_env="staging",
        justification="This capsule has passed all automated checks and peer review.",
        proposer_subject="proposer",
        policy_version="1",
    )
    proposal_payload = _json.dumps(proposal_pred).encode()
    envelope = sign_promote_envelope(proposal_payload, PROPOSAL_PAYLOAD_TYPE,
                                     proposer_key_path, proposer_cert_path)

    pred = build_approval_predicate(envelope, "approved", "approver")
    assert pred["role"] == "approver"
    assert pred["decision"] == "approved"
    assert "proposal_digest" in pred
    assert "sha256" in pred["proposal_digest"]
    assert len(pred["proposal_digest"]["sha256"]) == 64
    assert "timestamp" in pred


def test_build_approval_predicate_with_justification(
    proposer_key_path: Path, proposer_cert_path: Path
) -> None:
    proposal_payload = b'{"test": "data"}'
    envelope = sign_promote_envelope(proposal_payload, PROPOSAL_PAYLOAD_TYPE,
                                     proposer_key_path, proposer_cert_path)
    pred = build_approval_predicate(envelope, "rejected", "approver", justification="Not yet ready")
    assert pred["justification"] == "Not yet ready"


def test_build_policy_predicate_keys() -> None:
    pred = build_policy_predicate(["proposer"], ["approver"])
    assert pred["proposer_key_ids"] == ["proposer"]
    assert pred["approver_key_ids"] == ["approver"]
    assert pred["self_approval"] is False
    assert pred["approval_threshold"] == 1
    assert pred["bypass_valid_duration_hours"] == 24


def test_build_policy_predicate_custom_hours() -> None:
    pred = build_policy_predicate(["a"], ["b"], bypass_valid_duration_hours=48)
    assert pred["bypass_valid_duration_hours"] == 48


# ---------------------------------------------------------------------------
# Schema validation tests
# ---------------------------------------------------------------------------


def test_proposal_schema_valid() -> None:
    pred = {
        "role": "proposer",
        "capsule_id": "cap-001",
        "capsule_digest": {"sha256": "a" * 64},
        "target_environment": "staging",
        "justification": "This is a valid justification that is long enough.",
        "proposer_subject": "proposer",
        "policy_version": "1",
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    validate_predicate("promote_proposal_v1.json", pred)  # should not raise


def test_proposal_schema_missing_required() -> None:
    pred = {
        "role": "proposer",
        "capsule_id": "cap-001",
        # missing required fields
    }
    with pytest.raises(PredicateValidationError):
        validate_predicate("promote_proposal_v1.json", pred)


def test_proposal_schema_justification_too_short() -> None:
    pred = {
        "role": "proposer",
        "capsule_id": "cap-001",
        "capsule_digest": {"sha256": "a" * 64},
        "target_environment": "staging",
        "justification": "Too short",  # < 20 chars
        "proposer_subject": "proposer",
        "policy_version": "1",
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    with pytest.raises(PredicateValidationError):
        validate_predicate("promote_proposal_v1.json", pred)


def test_proposal_schema_wrong_role() -> None:
    pred = {
        "role": "approver",  # wrong const
        "capsule_id": "cap-001",
        "capsule_digest": {"sha256": "a" * 64},
        "target_environment": "staging",
        "justification": "This justification is long enough for testing.",
        "proposer_subject": "proposer",
        "policy_version": "1",
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    with pytest.raises(PredicateValidationError):
        validate_predicate("promote_proposal_v1.json", pred)


def test_approval_schema_valid() -> None:
    pred = {
        "role": "approver",
        "proposal_digest": {"sha256": "b" * 64},
        "decision": "approved",
        "approver_subject": "approver",
        "timestamp": "2026-01-01T01:00:00+00:00",
    }
    validate_predicate("promote_approval_v1.json", pred)  # should not raise


def test_approval_schema_invalid_decision() -> None:
    pred = {
        "role": "approver",
        "proposal_digest": {"sha256": "b" * 64},
        "decision": "maybe",  # not in enum
        "approver_subject": "approver",
        "timestamp": "2026-01-01T01:00:00+00:00",
    }
    with pytest.raises(PredicateValidationError):
        validate_predicate("promote_approval_v1.json", pred)


def test_policy_schema_valid() -> None:
    pred = {
        "proposer_key_ids": ["proposer"],
        "approver_key_ids": ["approver"],
        "self_approval": False,
        "approval_threshold": 1,
        "bypass_valid_duration_hours": 24,
    }
    validate_predicate("promote_policy_v1.json", pred)  # should not raise


def test_policy_schema_bypass_hours_too_high() -> None:
    pred = {
        "proposer_key_ids": ["proposer"],
        "approver_key_ids": ["approver"],
        "self_approval": False,
        "approval_threshold": 1,
        "bypass_valid_duration_hours": 200,  # > 168
    }
    with pytest.raises(PredicateValidationError):
        validate_predicate("promote_policy_v1.json", pred)


def test_policy_schema_self_approval_must_be_false() -> None:
    pred = {
        "proposer_key_ids": ["proposer"],
        "approver_key_ids": ["approver"],
        "self_approval": True,  # must be const false
        "approval_threshold": 1,
        "bypass_valid_duration_hours": 24,
    }
    with pytest.raises(PredicateValidationError):
        validate_predicate("promote_policy_v1.json", pred)


# ---------------------------------------------------------------------------
# sign_promote_envelope / verify_promote_envelope round-trip
# ---------------------------------------------------------------------------


def test_sign_verify_proposal_round_trip(
    proposer_key_path: Path, proposer_cert_path: Path
) -> None:
    payload = json.dumps({"hello": "world"}).encode()
    envelope = sign_promote_envelope(payload, PROPOSAL_PAYLOAD_TYPE,
                                     proposer_key_path, proposer_cert_path)
    recovered_payload, subject = verify_promote_envelope(envelope, PROPOSAL_PAYLOAD_TYPE)
    assert recovered_payload == payload
    assert subject == "proposer"


def test_verify_wrong_payload_type_raises(
    proposer_key_path: Path, proposer_cert_path: Path
) -> None:
    from novafabric.promote.predicates import EnvelopeError

    payload = json.dumps({"hello": "world"}).encode()
    envelope = sign_promote_envelope(payload, PROPOSAL_PAYLOAD_TYPE,
                                     proposer_key_path, proposer_cert_path)
    with pytest.raises(EnvelopeError, match="payloadType"):
        verify_promote_envelope(envelope, APPROVAL_PAYLOAD_TYPE)


def test_verify_tampered_signature_raises(
    proposer_key_path: Path, proposer_cert_path: Path
) -> None:
    from novafabric.promote.predicates import EnvelopeError

    payload = json.dumps({"hello": "world"}).encode()
    envelope = sign_promote_envelope(payload, PROPOSAL_PAYLOAD_TYPE,
                                     proposer_key_path, proposer_cert_path)
    env_dict = json.loads(envelope)
    env_dict["signatures"][0]["sig"] = "AAAA"  # corrupt
    with pytest.raises(EnvelopeError):
        verify_promote_envelope(json.dumps(env_dict).encode(), PROPOSAL_PAYLOAD_TYPE)


def test_verify_invalid_json_raises() -> None:
    from novafabric.promote.predicates import EnvelopeError

    with pytest.raises(EnvelopeError, match="not valid JSON"):
        verify_promote_envelope(b"not json at all", PROPOSAL_PAYLOAD_TYPE)


def test_verify_no_signatures_raises() -> None:
    from novafabric.promote.predicates import EnvelopeError

    env = json.dumps({
        "payload": "e30=",
        "payloadType": PROPOSAL_PAYLOAD_TYPE,
        "signatures": [],
    }).encode()
    with pytest.raises(EnvelopeError, match="no signatures"):
        verify_promote_envelope(env, PROPOSAL_PAYLOAD_TYPE)


def test_verify_corrupt_cert_raises(
    proposer_key_path: Path, proposer_cert_path: Path
) -> None:
    from novafabric.promote.predicates import EnvelopeError

    payload = json.dumps({"hello": "world"}).encode()
    envelope = sign_promote_envelope(payload, PROPOSAL_PAYLOAD_TYPE,
                                     proposer_key_path, proposer_cert_path)
    env_dict = json.loads(envelope)
    env_dict["signatures"][0]["cert"] = "AAAA"  # corrupt DER
    with pytest.raises(EnvelopeError, match="Failed to load cert"):
        verify_promote_envelope(json.dumps(env_dict).encode(), PROPOSAL_PAYLOAD_TYPE)


def test_sign_with_nonexistent_key_raises(proposer_cert_path: Path) -> None:
    from novafabric.promote.predicates import EnvelopeError

    with pytest.raises(EnvelopeError, match="Failed to load signing material"):
        sign_promote_envelope(
            b"payload", PROPOSAL_PAYLOAD_TYPE,
            Path("/nonexistent/key.pem"), proposer_cert_path,
        )


def test_load_non_ec_key_raises(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization as _ser
    from cryptography.hazmat.primitives.asymmetric import rsa

    from novafabric.promote.predicates import EnvelopeError, _load_private_key

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = rsa_key.private_bytes(
        _ser.Encoding.PEM, _ser.PrivateFormat.TraditionalOpenSSL, _ser.NoEncryption()
    )
    key_file = tmp_path / "rsa.pem"
    key_file.write_bytes(pem)
    with pytest.raises(EnvelopeError, match="not an EC private key"):
        _load_private_key(key_file)


def test_load_non_p256_ec_key_raises(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization as _ser
    from cryptography.hazmat.primitives.asymmetric import ec as _ec

    from novafabric.promote.predicates import EnvelopeError, _load_private_key

    p384_key = _ec.generate_private_key(_ec.SECP384R1())
    pem = p384_key.private_bytes(
        _ser.Encoding.PEM, _ser.PrivateFormat.TraditionalOpenSSL, _ser.NoEncryption()
    )
    key_file = tmp_path / "p384.pem"
    key_file.write_bytes(pem)
    with pytest.raises(EnvelopeError, match="requires P-256"):
        _load_private_key(key_file)


def test_validate_predicate_missing_schema_raises() -> None:
    from novafabric.promote.exceptions import PredicateValidationError

    with pytest.raises(PredicateValidationError, match="Schema not found"):
        validate_predicate("nonexistent_schema_v99.json", {"hello": "world"})


# ---------------------------------------------------------------------------
# _cert_subject fallback paths
# ---------------------------------------------------------------------------


def test_cert_subject_email_san_fallback() -> None:
    """_cert_subject uses email SAN when cert has no CN."""
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    from novafabric.promote.predicates import _cert_subject

    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([]))
        .issuer_name(x509.Name([]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.RFC822Name("user@example.com")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    assert _cert_subject(cert) == "user@example.com"


def test_cert_subject_rfc4514_fallback() -> None:
    """_cert_subject returns rfc4514 string when no CN and no email SAN."""
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    from novafabric.promote.predicates import _cert_subject

    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "TestOrg")]))
        .issuer_name(x509.Name([]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    subject = _cert_subject(cert)
    assert "TestOrg" in subject
