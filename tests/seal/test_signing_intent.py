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
    EnvelopeError,
    bind_intent,
    create_envelope,
    extract_intent,
    extract_payload,
    read_intent,
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


def _bound(obj: dict, intent: SigningIntent) -> bytes:
    """ADR-0309: an intent must be bound inside the payload before it is signed."""
    return json.dumps(bind_intent(obj, intent), sort_keys=True).encode()


class TestCreateEnvelopeWithIntent:
    def test_default_no_intent_has_no_intent_field(self, signing_materials):
        key_path, cert_path, _ = signing_materials
        payload = b'{"run_id": "x"}'
        env_bytes = create_envelope(payload, key_path, cert_path)
        env = json.loads(env_bytes)
        assert "intent" not in env["signatures"][0]

    def test_authored_intent_stored_in_signature(self, signing_materials):
        key_path, cert_path, _ = signing_materials
        payload = _bound({"run_id": "authored-test"}, SigningIntent.AUTHORED)
        env_bytes = create_envelope(payload, key_path, cert_path, intent=SigningIntent.AUTHORED)
        env = json.loads(env_bytes)
        assert env["signatures"][0]["intent"] == "authored"

    def test_approved_intent_stored_in_signature(self, signing_materials):
        key_path, cert_path, _ = signing_materials
        payload = _bound({"run_id": "approved-test"}, SigningIntent.APPROVED)
        env_bytes = create_envelope(payload, key_path, cert_path, intent=SigningIntent.APPROVED)
        env = json.loads(env_bytes)
        assert env["signatures"][0]["intent"] == "approved"

    def test_verified_intent_stored_in_signature(self, signing_materials):
        key_path, cert_path, _ = signing_materials
        payload = _bound({"check": True}, SigningIntent.VERIFIED)
        env_bytes = create_envelope(payload, key_path, cert_path, intent=SigningIntent.VERIFIED)
        env = json.loads(env_bytes)
        assert env["signatures"][0]["intent"] == "verified"

    def test_envelope_still_verifies_with_intent(self, signing_materials):
        key_path, cert_path, _ = signing_materials
        payload = _bound({"run_id": "verify-intent"}, SigningIntent.REVIEWED)
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
            _bound({"x": 1}, SigningIntent.AUTHORED), key_path, cert_path,
            intent=SigningIntent.AUTHORED,
        )
        assert extract_intent(env_bytes) is SigningIntent.AUTHORED

    def test_approved_roundtrip(self, signing_materials):
        key_path, cert_path, _ = signing_materials
        env_bytes = create_envelope(
            _bound({"x": 1}, SigningIntent.APPROVED), key_path, cert_path,
            intent=SigningIntent.APPROVED,
        )
        assert extract_intent(env_bytes) is SigningIntent.APPROVED

    def test_witnessed_roundtrip(self, signing_materials):
        key_path, cert_path, _ = signing_materials
        env_bytes = create_envelope(
            _bound({"x": 1}, SigningIntent.WITNESSED), key_path, cert_path,
            intent=SigningIntent.WITNESSED,
        )
        assert extract_intent(env_bytes) is SigningIntent.WITNESSED

    def test_unknown_intent_value_returns_none(self, signing_materials):
        """An unrecognised intent string should return None rather than raise."""
        key_path, cert_path, _ = signing_materials
        env_bytes = create_envelope(
            _bound({"x": 1}, SigningIntent.AUTHORED), key_path, cert_path,
            intent=SigningIntent.AUTHORED,
        )
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


# ---------------------------------------------------------------------------
# ADR-0309: the intent is signed, not only labelled
# ---------------------------------------------------------------------------


def _write_seal(capsule_dir, bundle, dsse=None):
    seal_dir = capsule_dir / ".seal"
    seal_dir.mkdir(parents=True, exist_ok=True)
    (seal_dir / "manifest.dsse").write_bytes(dsse or bundle.dsse_envelope)
    (seal_dir / "manifest.dsse.tsr").write_bytes(bundle.tsr)
    (seal_dir / "log-entry.json").write_text(json.dumps(bundle.log_entry), encoding="utf-8")
    return seal_dir


def _relabel(dsse: bytes, label: str) -> bytes:
    env = json.loads(dsse)
    env["signatures"][0]["intent"] = label
    return json.dumps(env).encode()


class TestIntentIsSigned:
    """Before ADR-0309 the intent label sat outside the DSSE PAE: editing
    "authored" to "approved" left the signature valid and the edited intent reported."""

    def test_the_seal_payload_carries_the_intent(self, nova_seal):
        bundle = nova_seal.seal({"run_id": "bound"}, intent=SigningIntent.APPROVED)
        payload = json.loads(extract_payload(bundle.dsse_envelope))
        assert payload["_novaseal"] == {"signing_intent": "approved"}
        assert read_intent(bundle.dsse_envelope).status == "signed"

    def test_an_edited_label_is_detected_and_fails_the_seal(self, nova_seal, tmp_path):
        bundle = nova_seal.seal({"run_id": "tampered"}, intent=SigningIntent.AUTHORED)
        forged = _relabel(bundle.dsse_envelope, "approved")
        assert verify_envelope(forged) is True  # the signature still verifies …
        assert extract_intent(forged) is None  # … but the edited label is not believed
        reading = read_intent(forged)
        assert (reading.status, reading.label) == ("mismatch", "approved")
        result = nova_seal.verify(bundle.capsule_id, str(_write_seal(tmp_path / "c", bundle, forged)))
        assert result.valid is False
        assert result.signing_intent is None
        assert any("ADR-0309" in e for e in result.errors)

    def test_an_intent_not_bound_in_the_payload_is_refused(self, signing_materials):
        key_path, cert_path, _ = signing_materials
        with pytest.raises(EnvelopeError, match="not bound in the payload"):
            create_envelope(b'{"x": 1}', key_path, cert_path, intent=SigningIntent.APPROVED)

    def test_a_pre_adr_0309_label_is_reported_as_unauthenticated(self, signing_materials):
        key_path, cert_path, _ = signing_materials
        legacy = _relabel(create_envelope(b'{"x": 1}', key_path, cert_path), "approved")
        assert verify_envelope(legacy) is True
        assert extract_intent(legacy) is None
        assert read_intent(legacy).status == "label-only"

    def test_verification_reports_the_status(self, nova_seal, tmp_path):
        bundle = nova_seal.seal({"run_id": "status"}, intent=SigningIntent.REVIEWED)
        result = nova_seal.verify(bundle.capsule_id, str(_write_seal(tmp_path / "s", bundle)))
        assert result.valid is True
        assert (result.signing_intent, result.signing_intent_status) == (
            SigningIntent.REVIEWED,
            "signed",
        )
