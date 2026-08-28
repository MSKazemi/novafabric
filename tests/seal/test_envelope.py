"""Tests for DSSE envelope creation and verification (NovaSeal v0.1)."""

from __future__ import annotations

import base64 as _base64
import json
import struct

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import ed25519 as _ed25519

from novafabric.trust.novaseal.envelope import (
    PAYLOAD_TYPE,
    EnvelopeError,
    _b64_decode,
    _b64_encode,
    _pae,
    create_envelope,
    extract_payload,
    verify_envelope,
)

# ---------------------------------------------------------------------------
# Fixtures — generate a temporary EC key pair and self-signed cert
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ec_key_pair(tmp_path_factory):
    """Generate a P-256 key pair and self-signed cert in a temp dir."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.x509.oid import NameOID

    tmp = tmp_path_factory.mktemp("keys")
    key = ec.generate_private_key(ec.SECP256R1())

    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key_path = tmp / "test.key"
    key_path.write_bytes(key_pem)

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "NovaSeal-Test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=365)
        )
        .sign(key, SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    cert_path = tmp / "test.crt"
    cert_path.write_bytes(cert_pem)

    return key_path, cert_path


# ---------------------------------------------------------------------------
# PAE
# ---------------------------------------------------------------------------

class TestPAE:
    def test_format(self):
        pae = _pae("text/plain", b"hello")
        assert pae.startswith(b"DSSEv1")

    def test_length_prefix_little_endian(self):
        payload_type = "text/plain"
        payload = b"hello"
        pae = _pae(payload_type, payload)
        # After b"DSSEv1": 8 bytes LE length of payload_type, then payload_type bytes,
        # then 8 bytes LE length of payload, then payload bytes
        offset = len(b"DSSEv1")
        pt_len = struct.unpack_from("<Q", pae, offset)[0]
        assert pt_len == len(payload_type.encode())
        offset += 8
        assert pae[offset:offset + pt_len] == payload_type.encode()
        offset += pt_len
        p_len = struct.unpack_from("<Q", pae, offset)[0]
        assert p_len == len(payload)
        offset += 8
        assert pae[offset:offset + p_len] == payload

    def test_empty_payload(self):
        pae = _pae(PAYLOAD_TYPE, b"")
        assert b"DSSEv1" in pae


# ---------------------------------------------------------------------------
# Base64url
# ---------------------------------------------------------------------------

class TestB64:
    """The DSSE spec puts *standard*, padded base64 on the wire (RFC 4648 §4).

    These tests replace three that asserted the opposite — ``test_no_padding``
    and ``test_url_safe`` pinned the URL-safe unpadded output that a stock
    verifier cannot read.  They encoded a wrong contract, so they are gone
    rather than adjusted.
    """

    def test_roundtrip(self):
        for data in [b"", b"hello", b"\x00\xff\xfe", b"a" * 100]:
            assert _b64_decode(_b64_encode(data)) == data

    def test_encoder_is_spec_compliant_standard_base64(self):
        data = bytes(range(256))
        encoded = _b64_encode(data)
        # standard alphabet, not URL-safe
        assert "-" not in encoded
        assert "_" not in encoded
        # and padded, so length is always a multiple of 4
        assert len(encoded) % 4 == 0
        assert _base64.b64decode(encoded) == data

    def test_a_stock_decoder_reads_our_envelope_payload(self):
        """The whole point of DSSE: someone else's tool must be able to read it."""
        payload = json.dumps(
            {"_type": "https://in-toto.io/Statement/v1", "subject": [{"name": "x"}]}
        ).encode()
        # exactly what a third-party verifier does — no NovaFabric code involved
        assert _base64.b64decode(_b64_encode(payload)) == payload

    @pytest.mark.parametrize(
        "encode",
        [
            # legacy NovaSeal: URL-safe, unpadded — must keep verifying
            lambda d: _base64.urlsafe_b64encode(d).rstrip(b"=").decode("ascii"),
            lambda d: _base64.urlsafe_b64encode(d).decode("ascii"),
            lambda d: _base64.b64encode(d).decode("ascii"),
            lambda d: _base64.b64encode(d).decode("ascii").rstrip("="),
        ],
        ids=["urlsafe-unpadded-legacy", "urlsafe-padded", "standard-padded", "standard-unpadded"],
    )
    def test_decoder_is_tolerant_of_every_encoding_in_the_wild(self, encode):
        for data in [b"", b"hello", bytes(range(256)), b"\xfb\xff\xfe", b"a" * 100]:
            assert _b64_decode(encode(data)) == data


# ---------------------------------------------------------------------------
# Envelope creation and verification
# ---------------------------------------------------------------------------

class TestCreateEnvelope:
    def test_creates_valid_json(self, ec_key_pair):
        key_path, cert_path = ec_key_pair
        payload = b'{"run_id": "test-123"}'
        env_bytes = create_envelope(payload, key_path, cert_path)
        env = json.loads(env_bytes)
        assert "payload" in env
        assert env["payloadType"] == PAYLOAD_TYPE
        assert len(env["signatures"]) == 1
        sig = env["signatures"][0]
        assert "keyid" in sig
        assert "sig" in sig
        assert "cert" in sig

    def test_payload_roundtrip(self, ec_key_pair):
        key_path, cert_path = ec_key_pair
        payload = b'{"test": true}'
        env_bytes = create_envelope(payload, key_path, cert_path)
        recovered = extract_payload(env_bytes)
        assert recovered == payload

    def test_wrong_curve_raises(self, tmp_path):
        key = ec.generate_private_key(ec.SECP384R1())  # P-384, not P-256
        key_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        key_path = tmp_path / "bad.key"
        key_path.write_bytes(key_pem)
        with pytest.raises(EnvelopeError, match="P-256"):
            create_envelope(b"test", key_path, tmp_path / "nonexistent.crt")

    def test_missing_key_raises(self, ec_key_pair, tmp_path):
        _, cert_path = ec_key_pair
        with pytest.raises(EnvelopeError):
            create_envelope(b"test", tmp_path / "no.key", cert_path)


class TestVerifyEnvelope:
    def test_valid_envelope(self, ec_key_pair):
        key_path, cert_path = ec_key_pair
        payload = b'{"run_id": "abc"}'
        env_bytes = create_envelope(payload, key_path, cert_path)
        assert verify_envelope(env_bytes) is True

    def test_tampered_payload_raises(self, ec_key_pair):
        key_path, cert_path = ec_key_pair
        payload = b'{"run_id": "abc"}'
        env_bytes = create_envelope(payload, key_path, cert_path)
        env = json.loads(env_bytes)
        # Tamper with the payload
        env["payload"] = _b64_encode(b'{"run_id": "TAMPERED"}')
        tampered = json.dumps(env).encode()
        with pytest.raises(EnvelopeError, match="[Ss]ignature"):
            verify_envelope(tampered)

    def test_expected_payload_match(self, ec_key_pair):
        key_path, cert_path = ec_key_pair
        payload = b'{"ok": 1}'
        env_bytes = create_envelope(payload, key_path, cert_path)
        assert verify_envelope(env_bytes, expected_payload=payload) is True

    def test_expected_payload_mismatch(self, ec_key_pair):
        key_path, cert_path = ec_key_pair
        payload = b'{"ok": 1}'
        env_bytes = create_envelope(payload, key_path, cert_path)
        with pytest.raises(EnvelopeError, match="payload does not match"):
            verify_envelope(env_bytes, expected_payload=b'{"ok": 2}')

    def test_invalid_json_raises(self):
        with pytest.raises(EnvelopeError, match="not valid JSON"):
            verify_envelope(b"not json")

    def test_no_signatures_raises(self, ec_key_pair):
        key_path, cert_path = ec_key_pair
        payload = b"test"
        env_bytes = create_envelope(payload, key_path, cert_path)
        env = json.loads(env_bytes)
        env["signatures"] = []
        with pytest.raises(EnvelopeError, match="no signatures"):
            verify_envelope(json.dumps(env).encode())

    def test_tampered_cert_raises(self, ec_key_pair):
        key_path, cert_path = ec_key_pair
        payload = b"test"
        env_bytes = create_envelope(payload, key_path, cert_path)
        env = json.loads(env_bytes)
        # Replace cert with garbage
        env["signatures"][0]["cert"] = _b64_encode(b"not a cert")
        with pytest.raises(EnvelopeError):
            verify_envelope(json.dumps(env).encode())


# ---------------------------------------------------------------------------
# Coverage: pubkey-field path (Go-collector style) + Ed25519 cert path +
# missing-field errors. These DSSE verification branches were previously
# untested (envelope.py 262-299, 303-305, 330-343).
# ---------------------------------------------------------------------------


def _b64u(data: bytes) -> str:
    return _base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _build_pubkey_envelope(private_key, public_key, payload: bytes) -> bytes:
    """Hand-build a DSSE envelope that carries a raw PEM public key in the
    'pubkey' field (the shape the Go collector emits) rather than an X.509 cert."""
    pae = _pae(PAYLOAD_TYPE, payload)
    if isinstance(private_key, ec.EllipticCurvePrivateKey):
        sig = private_key.sign(pae, ec.ECDSA(__import__("cryptography.hazmat.primitives.hashes", fromlist=["SHA256"]).SHA256()))
    else:
        sig = private_key.sign(pae)
    pubkey_pem = public_key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    env = {
        "payloadType": PAYLOAD_TYPE,
        "payload": _b64u(payload),
        "signatures": [{"sig": _b64u(sig), "pubkey": _b64u(pubkey_pem)}],
    }
    return json.dumps(env).encode("utf-8")


class TestEnvelopePubkeyField:
    def test_ed25519_pubkey_field_verifies(self):
        key = _ed25519.Ed25519PrivateKey.generate()
        payload = b'{"run":"ed25519-pubkey"}'
        env = _build_pubkey_envelope(key, key.public_key(), payload)
        assert verify_envelope(env, expected_payload=payload) is True

    def test_ecdsa_pubkey_field_verifies(self):
        key = ec.generate_private_key(ec.SECP256R1())
        payload = b'{"run":"ecdsa-pubkey"}'
        env = _build_pubkey_envelope(key, key.public_key(), payload)
        assert verify_envelope(env, expected_payload=payload) is True

    def test_pubkey_field_tampered_payload_raises(self):
        key = _ed25519.Ed25519PrivateKey.generate()
        env = _build_pubkey_envelope(key, key.public_key(), b'{"run":"orig"}')
        d = json.loads(env)
        d["payload"] = _b64u(b'{"run":"tampered"}')
        with pytest.raises(EnvelopeError, match="mismatch"):
            verify_envelope(json.dumps(d).encode("utf-8"))

    def test_unsupported_pubkey_type_raises(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pubkey_pem = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        env = {
            "payloadType": PAYLOAD_TYPE,
            "payload": _b64u(b"{}"),
            "signatures": [{"sig": _b64u(b"x"), "pubkey": _b64u(pubkey_pem)}],
        }
        with pytest.raises(EnvelopeError, match="Unsupported key type"):
            verify_envelope(json.dumps(env).encode("utf-8"))

    def test_missing_cert_and_pubkey_raises(self):
        env = {
            "payloadType": PAYLOAD_TYPE,
            "payload": _b64u(b"{}"),
            "signatures": [{"sig": _b64u(b"x")}],
        }
        with pytest.raises(EnvelopeError, match="no 'cert' or 'pubkey'"):
            verify_envelope(json.dumps(env).encode("utf-8"))


class TestEnvelopeEd25519Cert:
    def test_ed25519_cert_path_verifies(self, tmp_path):
        import datetime

        from cryptography import x509
        from cryptography.x509.oid import NameOID

        key = _ed25519.Ed25519PrivateKey.generate()
        key_path = tmp_path / "ed.key"
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "NovaSeal-Ed25519")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
            .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1))
            .sign(key, None)
        )
        cert_path = tmp_path / "ed.crt"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

        payload = b'{"run":"ed25519-cert"}'
        env = create_envelope(payload, key_path, cert_path)
        assert verify_envelope(env, expected_payload=payload) is True
