"""Tests for SigningIntent field in DSSE envelope (FDA 21 CFR §11.50(b)).

Verifies:
- Default intent (AUTHORED) is set correctly on NovaSeal.seal()
- Intent is round-tripped through create_envelope / extract_intent
- verify_envelope works with and without intent field present
- NovaSeal.verify() exposes intent via VerificationResult.signing_intent
- VerificationResult.__str__() includes intent when present
- Intent values round-trip through JSON serialisation (str-enum)
"""

from __future__ import annotations

import datetime
import json

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from novafabric.trust.novaseal import KeyConfig, NovaSeal, SigningIntent
from novafabric.trust.novaseal.envelope import (
    create_envelope,
    extract_intent,
    verify_envelope,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def signing_materials(tmp_path):
    """Generate a P-256 key pair and self-signed cert."""
    key = ec.generate_private_key(ec.SECP256R1())
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key_path = tmp_path / "seal.key"
    key_path.write_bytes(key_pem)

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "IntentTest")])
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
    cert_path = tmp_path / "seal.crt"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, cert_path, tmp_path


@pytest.fixture()
def nova_seal(signing_materials):
    key_path, cert_path, tmp_path = signing_materials
    config = KeyConfig(
        profile="local",
        key_path=str(key_path),
        cert_path=str(cert_path),
    )
    return NovaSeal(config=config, tsa_url="", db_path=str(tmp_path / "intent.db"))


# ---------------------------------------------------------------------------
# SigningIntent enum basics
# ---------------------------------------------------------------------------


class TestSigningIntentEnum:
    def test_all_values_are_strings(self):
        for member in SigningIntent:
            assert isinstance(member.value, str)
            assert member.value  # non-empty

    def test_authored_value(self):
        assert SigningIntent.AUTHORED.value == "authored"

    def test_approved_value(self):
        assert SigningIntent.APPROVED.value == "approved"

    def test_verified_value(self):
        assert SigningIntent.VERIFIED.value == "verified"

    def test_is_str_subclass(self):
        # SigningIntent(str, Enum) so it should compare equal to plain strings
        assert SigningIntent.AUTHORED == "authored"

    def test_from_string_roundtrip(self):
        for member in SigningIntent:
            assert SigningIntent(member.value) is member


# ---------------------------------------------------------------------------
# create_envelope with intent
# ---------------------------------------------------------------------------


class TestCreateEnvelopeWithIntent:
    def test_default_no_intent_has_no_intent_field(self, signing_materials):
        key_path, cert_path, _ = signing_materials
        payload = b'{"run_id": "x"}'
        env_bytes = create_envelope(payload, key_path, cert_path)
        env = json.loads(env_bytes)
        assert "intent" not in env["signatures"][0]

    def test_authored_intent_stored_in_signature(self, signing_materials):
        key_path, cert_path, _ = signing_materials
        payload = b'{"run_id": "authored-test"}'
        env_bytes = create_envelope(payload, key_path, cert_path, intent=SigningIntent.AUTHORED)
        env = json.loads(env_bytes)
        assert env["signatures"][0]["intent"] == "authored"

    def test_approved_intent_stored_in_signature(self, signing_materials):
        key_path, cert_path, _ = signing_materials
        payload = b'{"run_id": "approved-test"}'
        env_bytes = create_envelope(payload, key_path, cert_path, intent=SigningIntent.APPROVED)
        env = json.loads(env_bytes)
        assert env["signatures"][0]["intent"] == "approved"

    def test_verified_intent_stored_in_signature(self, signing_materials):
        key_path, cert_path, _ = signing_materials
        payload = b'{"check": true}'
        env_bytes = create_envelope(payload, key_path, cert_path, intent=SigningIntent.VERIFIED)
        env = json.loads(env_bytes)
        assert env["signatures"][0]["intent"] == "verified"

    def test_envelope_still_verifies_with_intent(self, signing_materials):
        key_path, cert_path, _ = signing_materials
        payload = b'{"run_id": "verify-intent"}'
        env_bytes = create_envelope(payload, key_path, cert_path, intent=SigningIntent.REVIEWED)
        assert verify_envelope(env_bytes) is True


# ---------------------------------------------------------------------------
# extract_intent round-trip
# ---------------------------------------------------------------------------


class TestExtractIntent:
    def test_no_intent_returns_none(self, signing_materials):
        key_path, cert_path, _ = signing_materials
        env_bytes = create_envelope(b'{"x": 1}', key_path, cert_path)
        assert extract_intent(env_bytes) is None

    def test_authored_roundtrip(self, signing_materials):
        key_path, cert_path, _ = signing_materials
        env_bytes = create_envelope(
            b'{"x": 1}', key_path, cert_path, intent=SigningIntent.AUTHORED
        )
        assert extract_intent(env_bytes) is SigningIntent.AUTHORED

    def test_approved_roundtrip(self, signing_materials):
        key_path, cert_path, _ = signing_materials
        env_bytes = create_envelope(
            b'{"x": 1}', key_path, cert_path, intent=SigningIntent.APPROVED
        )
        assert extract_intent(env_bytes) is SigningIntent.APPROVED

    def test_witnessed_roundtrip(self, signing_materials):
        key_path, cert_path, _ = signing_materials
        env_bytes = create_envelope(
            b'{"x": 1}', key_path, cert_path, intent=SigningIntent.WITNESSED
        )
        assert extract_intent(env_bytes) is SigningIntent.WITNESSED

    def test_unknown_intent_value_returns_none(self, signing_materials):
        """An unrecognised intent string should return None rather than raise."""
        key_path, cert_path, _ = signing_materials
        env_bytes = create_envelope(b'{"x": 1}', key_path, cert_path, intent=SigningIntent.AUTHORED)
        env = json.loads(env_bytes)
        env["signatures"][0]["intent"] = "custom_intent"
        modified = json.dumps(env).encode()
        assert extract_intent(modified) is None

    def test_malformed_envelope_returns_none(self):
        assert extract_intent(b"not json") is None

    def test_empty_bytes_returns_none(self):
        assert extract_intent(b"") is None


# ---------------------------------------------------------------------------
# NovaSeal.seal() default intent
# ---------------------------------------------------------------------------


class TestNovaSealDefaultIntent:
    def test_default_intent_is_authored(self, nova_seal):
        bundle = nova_seal.seal({"run_id": "default-intent"})
        intent = extract_intent(bundle.dsse_envelope)
        assert intent is SigningIntent.AUTHORED

    def test_explicit_authored_intent(self, nova_seal):
        bundle = nova_seal.seal({"run_id": "explicit-authored"}, intent=SigningIntent.AUTHORED)
        intent = extract_intent(bundle.dsse_envelope)
        assert intent is SigningIntent.AUTHORED

    def test_approved_intent_on_seal(self, nova_seal):
        bundle = nova_seal.seal({"run_id": "approved"}, intent=SigningIntent.APPROVED)
        intent = extract_intent(bundle.dsse_envelope)
        assert intent is SigningIntent.APPROVED

    def test_none_intent_omits_field(self, nova_seal):
        bundle = nova_seal.seal({"run_id": "no-intent"}, intent=None)
        intent = extract_intent(bundle.dsse_envelope)
        assert intent is None


# ---------------------------------------------------------------------------
# NovaSeal.verify() exposes intent
# ---------------------------------------------------------------------------


class TestVerifyResultIntent:
    def _write_bundle(self, capsule_dir, bundle):
        import json as _json
        seal_dir = capsule_dir / ".seal"
        seal_dir.mkdir(exist_ok=True)
        (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
        (seal_dir / "manifest.dsse.tsr").write_bytes(bundle.tsr)
        (seal_dir / "log-entry.json").write_text(
            _json.dumps(bundle.log_entry, indent=2), encoding="utf-8"
        )
        return seal_dir

    def test_verify_result_has_authored_intent(self, nova_seal, tmp_path):
        bundle = nova_seal.seal({"run_id": "vr-authored"}, intent=SigningIntent.AUTHORED)
        capsule_dir = tmp_path / "cap-vr-authored"
        capsule_dir.mkdir()
        seal_dir = self._write_bundle(capsule_dir, bundle)

        result = nova_seal.verify(bundle.capsule_id, str(seal_dir))
        assert result.valid is True
        assert result.signing_intent is SigningIntent.AUTHORED

    def test_verify_result_has_approved_intent(self, nova_seal, tmp_path):
        bundle = nova_seal.seal({"run_id": "vr-approved"}, intent=SigningIntent.APPROVED)
        capsule_dir = tmp_path / "cap-vr-approved"
        capsule_dir.mkdir()
        seal_dir = self._write_bundle(capsule_dir, bundle)

        result = nova_seal.verify(bundle.capsule_id, str(seal_dir))
        assert result.valid is True
        assert result.signing_intent is SigningIntent.APPROVED

    def test_verify_result_none_intent_when_omitted(self, nova_seal, tmp_path):
        bundle = nova_seal.seal({"run_id": "vr-no-intent"}, intent=None)
        capsule_dir = tmp_path / "cap-vr-no-intent"
        capsule_dir.mkdir()
        seal_dir = self._write_bundle(capsule_dir, bundle)

        result = nova_seal.verify(bundle.capsule_id, str(seal_dir))
        assert result.valid is True
        assert result.signing_intent is None

    def test_str_includes_intent_when_present(self, nova_seal, tmp_path):
        bundle = nova_seal.seal({"run_id": "str-intent"}, intent=SigningIntent.APPROVED)
        capsule_dir = tmp_path / "cap-str-intent"
        capsule_dir.mkdir()
        seal_dir = self._write_bundle(capsule_dir, bundle)

        result = nova_seal.verify(bundle.capsule_id, str(seal_dir))
        s = str(result)
        assert "intent=approved" in s
        assert "signature_ok=True" in s

    def test_str_excludes_intent_when_absent(self, nova_seal, tmp_path):
        bundle = nova_seal.seal({"run_id": "str-no-intent"}, intent=None)
        capsule_dir = tmp_path / "cap-str-no-intent"
        capsule_dir.mkdir()
        seal_dir = self._write_bundle(capsule_dir, bundle)

        result = nova_seal.verify(bundle.capsule_id, str(seal_dir))
        assert "intent=" not in str(result)
