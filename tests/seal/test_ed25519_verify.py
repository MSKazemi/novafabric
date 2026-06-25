"""Tests for NovaSeal Ed25519 verification support (A-7)."""

from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from novafabric.trust.novaseal.envelope import (
    PAYLOAD_TYPE,
    EnvelopeError,
    _b64url_decode,
    _b64url_encode,
    _pae,
    create_envelope,
    verify_envelope,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ed25519_keypair():
    """Generate an Ed25519 key pair and return (private_key, public_key)."""
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    return private_key, public_key


def _public_key_to_pem(public_key) -> bytes:
    return public_key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _build_ed25519_envelope(payload: bytes, private_key, public_key) -> bytes:
    """Build a DSSE envelope signed with an Ed25519 key.

    Embeds the public key as a PEM-encoded value in the 'pubkey' field
    (the Go collector format — no X.509 cert needed).
    """
    pae = _pae(PAYLOAD_TYPE, payload)
    signature = private_key.sign(pae)

    pubkey_pem = _public_key_to_pem(public_key)

    sig_entry = {
        "keyid": "ed25519-test",
        "sig": _b64url_encode(signature),
        "pubkey": _b64url_encode(pubkey_pem),
    }

    envelope = {
        "payload": _b64url_encode(payload),
        "payloadType": PAYLOAD_TYPE,
        "signatures": [sig_entry],
    }
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# Tests: Ed25519 verification via 'pubkey' field
# ---------------------------------------------------------------------------


class TestEd25519VerifyViaPublicKeyField:
    def test_valid_ed25519_envelope_verifies(self) -> None:
        """A valid Ed25519-signed DSSE envelope must verify to True."""
        priv, pub = _make_ed25519_keypair()
        payload = b'{"run_id": "test-ed25519", "status": "completed"}'
        envelope_bytes = _build_ed25519_envelope(payload, priv, pub)

        assert verify_envelope(envelope_bytes) is True

    def test_payload_mismatch_raises(self) -> None:
        """verify_envelope raises when expected_payload does not match."""
        priv, pub = _make_ed25519_keypair()
        payload = b'{"run_id": "r1"}'
        envelope_bytes = _build_ed25519_envelope(payload, priv, pub)

        with pytest.raises(EnvelopeError, match="payload does not match"):
            verify_envelope(envelope_bytes, expected_payload=b'{"run_id": "r2"}')

    def test_tampered_signature_raises(self) -> None:
        """A tampered signature in an Ed25519 envelope raises EnvelopeError."""
        priv, pub = _make_ed25519_keypair()
        payload = b'{"run_id": "r1"}'
        envelope_bytes = _build_ed25519_envelope(payload, priv, pub)

        env = json.loads(envelope_bytes)
        # Flip a byte in the signature
        sig_bytes = _b64url_decode(env["signatures"][0]["sig"])
        tampered = bytearray(sig_bytes)
        tampered[0] ^= 0xFF
        env["signatures"][0]["sig"] = _b64url_encode(bytes(tampered))

        tampered_envelope = json.dumps(env, separators=(",", ":")).encode()
        with pytest.raises(EnvelopeError, match="signature"):
            verify_envelope(tampered_envelope)

    def test_tampered_payload_raises(self) -> None:
        """Tampering with the payload bytes invalidates the Ed25519 signature."""
        priv, pub = _make_ed25519_keypair()
        payload = b'{"run_id": "r1"}'
        envelope_bytes = _build_ed25519_envelope(payload, priv, pub)

        env = json.loads(envelope_bytes)
        # Replace payload with a different value
        env["payload"] = _b64url_encode(b'{"run_id": "TAMPERED"}')
        tampered_envelope = json.dumps(env, separators=(",", ":")).encode()

        with pytest.raises(EnvelopeError, match="signature"):
            verify_envelope(tampered_envelope)

    def test_wrong_public_key_raises(self) -> None:
        """Using a different Ed25519 public key fails verification."""
        priv1, _ = _make_ed25519_keypair()
        _, pub2 = _make_ed25519_keypair()  # Different key pair

        payload = b'{"run_id": "r1"}'
        pae = _pae(PAYLOAD_TYPE, payload)
        signature = priv1.sign(pae)

        # Build envelope with pub2 instead of pub1
        pubkey_pem = _public_key_to_pem(pub2)
        sig_entry = {
            "keyid": "ed25519-wrong",
            "sig": _b64url_encode(signature),
            "pubkey": _b64url_encode(pubkey_pem),
        }
        envelope = {
            "payload": _b64url_encode(payload),
            "payloadType": PAYLOAD_TYPE,
            "signatures": [sig_entry],
        }
        envelope_bytes = json.dumps(envelope, separators=(",", ":")).encode()

        with pytest.raises(EnvelopeError, match="signature"):
            verify_envelope(envelope_bytes)


# ---------------------------------------------------------------------------
# Tests: existing ECDSA P-256 path still works (regression)
# ---------------------------------------------------------------------------


class TestEcdsaStillWorks:
    def test_ecdsa_envelope_still_verifies(self, tmp_path) -> None:
        """The original ECDSA P-256 create_envelope/verify_envelope round-trip works."""
        import datetime

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes as _hashes
        from cryptography.x509.oid import NameOID

        # Generate ECDSA P-256 key + self-signed cert
        priv_key = ec.generate_private_key(ec.SECP256R1())
        pub_key = priv_key.public_key()

        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "novaseal-test"),
        ])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(pub_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow())
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=1))
            .sign(priv_key, _hashes.SHA256())
        )

        key_path = tmp_path / "key.pem"
        cert_path = tmp_path / "cert.pem"

        key_path.write_bytes(
            priv_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        cert_path.write_bytes(
            cert.public_bytes(serialization.Encoding.PEM)
        )

        payload = b'{"run_id": "ecdsa-regression"}'
        envelope_bytes = create_envelope(payload, key_path, cert_path)
        assert verify_envelope(envelope_bytes) is True
        assert verify_envelope(envelope_bytes, expected_payload=payload) is True


# ---------------------------------------------------------------------------
# Tests: missing cert and pubkey field
# ---------------------------------------------------------------------------


class TestMissingKeyFields:
    def test_raises_when_neither_cert_nor_pubkey(self) -> None:
        """Envelope with no 'cert' or 'pubkey' field raises EnvelopeError."""
        priv, pub = _make_ed25519_keypair()
        payload = b'{"run_id": "r1"}'
        pae = _pae(PAYLOAD_TYPE, payload)
        signature = priv.sign(pae)

        sig_entry = {
            "keyid": "no-key-field",
            "sig": _b64url_encode(signature),
            # No 'cert' and no 'pubkey'
        }
        envelope = {
            "payload": _b64url_encode(payload),
            "payloadType": PAYLOAD_TYPE,
            "signatures": [sig_entry],
        }
        envelope_bytes = json.dumps(envelope, separators=(",", ":")).encode()

        with pytest.raises(EnvelopeError, match="no 'cert' or 'pubkey'"):
            verify_envelope(envelope_bytes)

    def test_no_signatures_raises(self) -> None:
        """Envelope with empty signatures list raises EnvelopeError."""
        envelope = {
            "payload": _b64url_encode(b"data"),
            "payloadType": PAYLOAD_TYPE,
            "signatures": [],
        }
        with pytest.raises(EnvelopeError, match="no signatures"):
            verify_envelope(json.dumps(envelope).encode())
