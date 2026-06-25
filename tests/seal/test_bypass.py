"""Tests for bypass predicate, bundle store, and verifier bypass logic."""

from __future__ import annotations

import datetime
import json
from datetime import UTC, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from novafabric.cli.seal_propose import _parse_duration_hours
from novafabric.promote.bundle_store import PromoteBundleStore
from novafabric.promote.exceptions import BundleNotFoundError, PredicateValidationError
from novafabric.promote.predicates import (
    BYPASS_PAYLOAD_TYPE,
    build_bypass_predicate,
    sign_promote_envelope,
    validate_predicate,
    verify_promote_envelope,
)

# ---------------------------------------------------------------------------
# Key/cert generation helper
# ---------------------------------------------------------------------------

def _gen_key_cert(tmp_path, cn: str = "TestOperator"):
    """Generate ECDSA P-256 key + self-signed cert; return (key_path, cert_path)."""
    key = ec.generate_private_key(ec.SECP256R1())
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key_path = tmp_path / f"{cn}.key"
    key_path.write_bytes(key_pem)

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / f"{cn}.crt"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, cert_path


LONG_REASON = "This is an emergency bypass for production incident INC-00123 affecting all users." * 1


# ---------------------------------------------------------------------------
# Predicate tests
# ---------------------------------------------------------------------------

class TestBypassPredicate:
    def test_bypass_predicate_valid(self, tmp_path):
        """build_bypass_predicate produces a dict that passes schema validation."""
        import hashlib
        from datetime import datetime

        capsule_id = "cap-abc-123"
        digest = hashlib.sha256(capsule_id.encode()).hexdigest()
        valid_until = (datetime.now(UTC) + timedelta(hours=24)).isoformat()

        predicate = build_bypass_predicate(
            capsule_id=capsule_id,
            capsule_digest_hex=digest,
            target_environment="production",
            bypass_reason=LONG_REASON,
            bypass_authorized_by="operator@example.com",
            valid_until=valid_until,
            notification_sent_to=["oncall@example.com"],
            notification_status="sent",
        )
        # Should not raise
        validate_predicate("promote_bypass_v1.json", predicate)

    def test_bypass_predicate_reason_too_short(self, tmp_path):
        """validate_predicate raises PredicateValidationError when reason < 50 chars."""
        import hashlib
        from datetime import datetime

        capsule_id = "cap-abc-123"
        digest = hashlib.sha256(capsule_id.encode()).hexdigest()
        valid_until = (datetime.now(UTC) + timedelta(hours=24)).isoformat()

        predicate = build_bypass_predicate(
            capsule_id=capsule_id,
            capsule_digest_hex=digest,
            target_environment="production",
            bypass_reason="Too short",
            bypass_authorized_by="operator@example.com",
            valid_until=valid_until,
            notification_sent_to=[],
        )
        with pytest.raises(PredicateValidationError):
            validate_predicate("promote_bypass_v1.json", predicate)

    def test_bypass_predicate_notification_status_field(self):
        """build_bypass_predicate includes notification_status."""
        import hashlib
        from datetime import datetime

        capsule_id = "cap-xyz"
        digest = hashlib.sha256(capsule_id.encode()).hexdigest()
        valid_until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

        predicate = build_bypass_predicate(
            capsule_id=capsule_id,
            capsule_digest_hex=digest,
            target_environment="staging",
            bypass_reason=LONG_REASON,
            bypass_authorized_by="op",
            valid_until=valid_until,
            notification_sent_to=[],
            notification_status="not_configured",
        )
        assert predicate["notification_status"] == "not_configured"


# ---------------------------------------------------------------------------
# Bundle store tests
# ---------------------------------------------------------------------------

class TestBundleStoreBypass:
    def test_bundle_store_put_get_bypass(self, tmp_path):
        """Round-trip: put_bypass returns UUID; get_bypass returns original bytes."""
        store = PromoteBundleStore(tmp_path)
        data = b'{"hello": "world"}'
        valid_until = "2099-01-01T00:00:00+00:00"

        bypass_uuid = store.put_bypass("cap-1", data, valid_until)
        assert isinstance(bypass_uuid, str)
        assert len(bypass_uuid) == 36  # UUID format

        retrieved = store.get_bypass("cap-1", bypass_uuid)
        assert retrieved == data

    def test_bundle_store_list_bypasses(self, tmp_path):
        """list_bypasses returns UUIDs for stored bypasses."""
        store = PromoteBundleStore(tmp_path)
        valid_until = "2099-01-01T00:00:00+00:00"

        uuid1 = store.put_bypass("cap-2", b"env1", valid_until)
        uuid2 = store.put_bypass("cap-2", b"env2", valid_until)

        bypasses = store.list_bypasses("cap-2")
        assert sorted([uuid1, uuid2]) == bypasses

    def test_bundle_store_bypass_not_found(self, tmp_path):
        """get_bypass raises BundleNotFoundError for unknown UUID."""
        store = PromoteBundleStore(tmp_path)
        with pytest.raises(BundleNotFoundError):
            store.get_bypass("cap-3", "nonexistent-uuid")

    def test_bundle_store_list_bypasses_empty(self, tmp_path):
        """list_bypasses returns empty list when no bypasses exist."""
        store = PromoteBundleStore(tmp_path)
        assert store.list_bypasses("cap-unknown") == []

    def test_bundle_store_get_bypass_valid_until(self, tmp_path):
        """get_bypass_valid_until returns the stored ISO string."""
        store = PromoteBundleStore(tmp_path)
        valid_until = "2099-06-15T12:00:00+00:00"
        bypass_uuid = store.put_bypass("cap-4", b"data", valid_until)
        assert store.get_bypass_valid_until("cap-4", bypass_uuid) == valid_until

    def test_bundle_store_get_bypass_valid_until_not_found(self, tmp_path):
        """get_bypass_valid_until raises BundleNotFoundError for unknown UUID."""
        store = PromoteBundleStore(tmp_path)
        with pytest.raises(BundleNotFoundError):
            store.get_bypass_valid_until("cap-5", "no-such-uuid")


# ---------------------------------------------------------------------------
# Sign / verify tests
# ---------------------------------------------------------------------------

class TestBypassSignVerify:
    def test_bypass_sign_verify(self, tmp_path):
        """sign_promote_envelope + verify_promote_envelope round-trip with BYPASS_PAYLOAD_TYPE."""
        import hashlib
        from datetime import datetime

        key_path, cert_path = _gen_key_cert(tmp_path)
        capsule_id = "cap-sign-test"
        digest = hashlib.sha256(capsule_id.encode()).hexdigest()
        valid_until = (datetime.now(UTC) + timedelta(hours=4)).isoformat()

        predicate = build_bypass_predicate(
            capsule_id=capsule_id,
            capsule_digest_hex=digest,
            target_environment="production",
            bypass_reason=LONG_REASON,
            bypass_authorized_by="TestOperator",
            valid_until=valid_until,
            notification_sent_to=[],
        )
        payload = json.dumps(predicate).encode()
        envelope = sign_promote_envelope(payload, BYPASS_PAYLOAD_TYPE, key_path, cert_path)

        recovered_payload, subject = verify_promote_envelope(envelope, BYPASS_PAYLOAD_TYPE)
        assert json.loads(recovered_payload)["capsule_id"] == capsule_id
        assert subject == "TestOperator"

    def test_bypass_wrong_payload_type_rejected(self, tmp_path):
        """verify_promote_envelope raises EnvelopeError when payloadType mismatches."""
        from novafabric.promote.predicates import PROPOSAL_PAYLOAD_TYPE, EnvelopeError

        key_path, cert_path = _gen_key_cert(tmp_path)
        payload = b'{"test": true}'
        envelope = sign_promote_envelope(payload, BYPASS_PAYLOAD_TYPE, key_path, cert_path)

        with pytest.raises(EnvelopeError):
            verify_promote_envelope(envelope, PROPOSAL_PAYLOAD_TYPE)


# ---------------------------------------------------------------------------
# Verifier bypass logic tests
# ---------------------------------------------------------------------------

class TestVerifySodBypass:
    def _make_policy_store(self, tmp_path, key_path, cert_path):
        """Create a policy store with a valid policy including the cert CN."""
        from novafabric.promote.policy_store import PolicyStore
        from novafabric.promote.predicates import (
            POLICY_PAYLOAD_TYPE,
            build_policy_predicate,
            sign_promote_envelope,
        )

        policy_store = PolicyStore(tmp_path / "policy.db")
        predicate = build_policy_predicate(
            proposer_key_ids=["proposer"],
            approver_key_ids=["approver"],
        )
        payload = json.dumps(predicate).encode()
        envelope = sign_promote_envelope(payload, POLICY_PAYLOAD_TYPE, key_path, cert_path)
        policy_store.put(envelope.decode("utf-8"))
        return policy_store

    def test_verify_sod_valid_bypass_short_circuits(self, tmp_path):
        """verify_sod returns bypass_used=True when a valid (non-expired) bypass exists."""
        import hashlib
        from datetime import datetime

        from novafabric.promote.verifier import verify_sod

        key_path, cert_path = _gen_key_cert(tmp_path, "BypassOperator")
        capsule_id = "cap-bypass-active"

        valid_until = (datetime.now(UTC) + timedelta(hours=12)).isoformat()
        digest = hashlib.sha256(capsule_id.encode()).hexdigest()
        predicate = build_bypass_predicate(
            capsule_id=capsule_id,
            capsule_digest_hex=digest,
            target_environment="production",
            bypass_reason=LONG_REASON,
            bypass_authorized_by="BypassOperator",
            valid_until=valid_until,
            notification_sent_to=[],
        )
        payload = json.dumps(predicate).encode()
        envelope = sign_promote_envelope(payload, BYPASS_PAYLOAD_TYPE, key_path, cert_path)

        bundle_store = PromoteBundleStore(tmp_path)
        bundle_store.put_bypass(capsule_id, envelope, valid_until)

        policy_store = self._make_policy_store(tmp_path, key_path, cert_path)

        result = verify_sod(capsule_id, policy_store, bundle_store)
        assert result.passed is True
        assert result.bypass_used is True
        assert result.exit_code == 0
        assert "bypass active" in result.message

    def test_verify_sod_expired_bypass_falls_through(self, tmp_path):
        """Expired bypass falls through to 5-check SoD (fails with exit_code=9 — no proposal)."""
        import hashlib
        from datetime import datetime

        from novafabric.promote.verifier import verify_sod

        key_path, cert_path = _gen_key_cert(tmp_path, "ExpiredOp")
        capsule_id = "cap-expired-bypass"

        # expired 1 hour ago
        valid_until = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        digest = hashlib.sha256(capsule_id.encode()).hexdigest()
        predicate = build_bypass_predicate(
            capsule_id=capsule_id,
            capsule_digest_hex=digest,
            target_environment="production",
            bypass_reason=LONG_REASON,
            bypass_authorized_by="ExpiredOp",
            valid_until=valid_until,
            notification_sent_to=[],
        )
        payload = json.dumps(predicate).encode()
        envelope = sign_promote_envelope(payload, BYPASS_PAYLOAD_TYPE, key_path, cert_path)

        bundle_store = PromoteBundleStore(tmp_path)
        bundle_store.put_bypass(capsule_id, envelope, valid_until)

        policy_store = self._make_policy_store(tmp_path, key_path, cert_path)

        result = verify_sod(capsule_id, policy_store, bundle_store)
        assert result.passed is False
        assert result.bypass_used is False
        # No proposal → exit_code 9
        assert result.exit_code == 9

    def test_verify_sod_bypass_invalid_signature_skipped(self, tmp_path):
        """Corrupted bypass envelope is skipped; falls through to SoD (exit_code=9)."""
        from datetime import datetime

        from novafabric.promote.verifier import verify_sod

        key_path, cert_path = _gen_key_cert(tmp_path, "CorruptOp")
        capsule_id = "cap-corrupt-bypass"

        valid_until = (datetime.now(UTC) + timedelta(hours=24)).isoformat()
        bundle_store = PromoteBundleStore(tmp_path)

        # Store a corrupted (non-valid DSSE) envelope
        corrupted = b'{"payload": "bad", "payloadType": "wrong", "signatures": []}'
        bundle_store.put_bypass(capsule_id, corrupted, valid_until)

        policy_store = self._make_policy_store(tmp_path, key_path, cert_path)

        result = verify_sod(capsule_id, policy_store, bundle_store)
        assert result.passed is False
        assert result.bypass_used is False
        # Fell through to no-proposal check
        assert result.exit_code == 9


# ---------------------------------------------------------------------------
# Duration parsing tests
# ---------------------------------------------------------------------------

class TestParseDurationHours:
    def test_bypass_duration_parse_hours(self):
        assert _parse_duration_hours("24h") == 24

    def test_bypass_duration_parse_days(self):
        assert _parse_duration_hours("7d") == 168

    def test_bypass_duration_parse_1h(self):
        assert _parse_duration_hours("1h") == 1

    def test_bypass_duration_parse_3d(self):
        assert _parse_duration_hours("3d") == 72

    def test_bypass_duration_parse_invalid(self):
        with pytest.raises(ValueError, match="Invalid duration"):
            _parse_duration_hours("foo")

    def test_bypass_duration_parse_invalid_format(self):
        with pytest.raises(ValueError, match="Invalid duration"):
            _parse_duration_hours("24")

    def test_bypass_duration_parse_case_insensitive(self):
        assert _parse_duration_hours("24H") == 24
        assert _parse_duration_hours("7D") == 168
