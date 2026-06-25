"""Integration tests: NovaSeal sign → verify round-trip."""

from __future__ import annotations

import datetime
import json

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from novafabric.trust.novaseal import KeyConfig, NovaSeal, SealBundle, VerificationResult

# ---------------------------------------------------------------------------
# Fixture: key pair + self-signed cert written to tmp dir
# ---------------------------------------------------------------------------

@pytest.fixture()
def signing_materials(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key_path = tmp_path / "seal.key"
    key_path.write_bytes(key_pem)

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "NovaSeal-IntegrationTest")])
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
    return NovaSeal(
        config=config,
        tsa_url="",  # disable TSA in tests (network not available)
        db_path=str(tmp_path / "merkle.db"),
    )


# ---------------------------------------------------------------------------
# SealBundle structure
# ---------------------------------------------------------------------------

class TestSealBundle:
    def test_seal_returns_bundle(self, nova_seal):
        manifest = {"run_id": "test-001", "status": "success"}
        bundle = nova_seal.seal(manifest)
        assert isinstance(bundle, SealBundle)
        assert bundle.dsse_envelope
        assert isinstance(bundle.log_entry, dict)
        assert len(bundle.capsule_id) == 64  # SHA-256 hex

    def test_seal_tsr_empty_when_no_tsa(self, nova_seal):
        manifest = {"run_id": "test-002"}
        bundle = nova_seal.seal(manifest)
        assert bundle.tsr == b""

    def test_seal_log_entry_fields(self, nova_seal):
        manifest = {"run_id": "test-003"}
        bundle = nova_seal.seal(manifest)
        le = bundle.log_entry
        assert "leaf_index" in le
        assert "leaf_hash" in le
        assert "root_hash" in le
        assert "tree_size" in le
        assert "entry" in le

    def test_capsule_id_deterministic(self, nova_seal):
        import hashlib
        import json
        manifest = {"run_id": "test-004"}
        payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        expected_id = hashlib.sha256(payload).hexdigest()
        bundle = nova_seal.seal(manifest)
        assert bundle.capsule_id == expected_id


# ---------------------------------------------------------------------------
# Verify round-trip
# ---------------------------------------------------------------------------

class TestVerifyRoundTrip:
    def _write_bundle(self, capsule_dir, bundle: SealBundle) -> None:
        seal_dir = capsule_dir / ".seal"
        seal_dir.mkdir(exist_ok=True)
        (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
        (seal_dir / "manifest.dsse.tsr").write_bytes(bundle.tsr)
        (seal_dir / "log-entry.json").write_text(
            json.dumps(bundle.log_entry, indent=2), encoding="utf-8"
        )

    def test_sign_verify_success(self, nova_seal, tmp_path):
        manifest = {"run_id": "verify-001", "model": "gpt-4o"}
        bundle = nova_seal.seal(manifest)

        capsule_dir = tmp_path / "capsule-verify-001"
        capsule_dir.mkdir()
        self._write_bundle(capsule_dir, bundle)

        result = nova_seal.verify(
            capsule_id=bundle.capsule_id,
            seal_dir=str(capsule_dir / ".seal"),
        )
        assert isinstance(result, VerificationResult)
        assert result.signature_ok is True
        assert result.timestamp_ok is True  # no TSR = ok
        assert result.log_integrity_ok is True
        assert result.valid is True
        assert result.errors == []

    def test_verify_output_str(self, nova_seal, tmp_path):
        manifest = {"run_id": "str-001"}
        bundle = nova_seal.seal(manifest)
        capsule_dir = tmp_path / "capsule-str-001"
        capsule_dir.mkdir()
        self._write_bundle(capsule_dir, bundle)

        result = nova_seal.verify(bundle.capsule_id, str(capsule_dir / ".seal"))
        assert "signature_ok=True" in str(result)
        assert "timestamp_ok=True" in str(result)
        assert "log_integrity_ok=True" in str(result)

    def test_verify_missing_dsse_fails(self, nova_seal, tmp_path):
        manifest = {"run_id": "missing-dsse"}
        bundle = nova_seal.seal(manifest)
        capsule_dir = tmp_path / "capsule-missing"
        capsule_dir.mkdir()
        self._write_bundle(capsule_dir, bundle)
        (capsule_dir / ".seal" / "manifest.dsse").unlink()

        result = nova_seal.verify(bundle.capsule_id, str(capsule_dir / ".seal"))
        assert result.signature_ok is False
        assert result.valid is False

    def test_verify_tampered_dsse_fails(self, nova_seal, tmp_path):
        manifest = {"run_id": "tampered"}
        bundle = nova_seal.seal(manifest)
        capsule_dir = tmp_path / "capsule-tampered"
        capsule_dir.mkdir()
        self._write_bundle(capsule_dir, bundle)

        # Tamper with the DSSE envelope
        dsse_path = capsule_dir / ".seal" / "manifest.dsse"
        original = json.loads(dsse_path.read_bytes())
        original["payload"] = "dGFtcGVyZWQ"  # base64url("tampered")
        dsse_path.write_bytes(json.dumps(original).encode())

        result = nova_seal.verify(bundle.capsule_id, str(capsule_dir / ".seal"))
        assert result.signature_ok is False
        assert result.valid is False

    def test_multiple_seals_increment_merkle(self, nova_seal, tmp_path):
        for i in range(3):
            manifest = {"run_id": f"multi-{i}"}
            bundle = nova_seal.seal(manifest)
            assert bundle.log_entry["leaf_index"] == i
            assert bundle.log_entry["tree_size"] == i + 1

    def test_key_rotation(self, signing_materials, tmp_path):
        key_path, cert_path, mat_tmp = signing_materials

        config = KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path))
        seal = NovaSeal(config=config, tsa_url="", db_path=str(tmp_path / "rot.db"))

        # Seal one capsule with the old key
        bundle1 = seal.seal({"run_id": "before-rotation"})
        assert bundle1.log_entry["leaf_index"] == 0

        # Generate a new key and cert
        new_key = ec.generate_private_key(ec.SECP256R1())
        new_key_path = tmp_path / "new.key"
        new_key_path.write_bytes(
            new_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "NovaSeal-New")])
        new_cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(new_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
            .not_valid_after(
                datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
            )
            .sign(new_key, hashes.SHA256())
        )
        new_cert_path = tmp_path / "new.crt"
        new_cert_path.write_bytes(new_cert.public_bytes(serialization.Encoding.PEM))

        new_config = KeyConfig(
            profile="local",
            key_path=str(new_key_path),
            cert_path=str(new_cert_path),
        )
        receipt = seal.rotate_key(new_config)

        assert receipt.old_key_fingerprint != receipt.new_key_fingerprint
        assert "leaf_index" in receipt.rotation_log_entry
        # Rotation entry goes into log at index 1
        assert receipt.rotation_log_entry["leaf_index"] == 1

        # Seal another capsule after rotation
        bundle2 = seal.seal({"run_id": "after-rotation"})
        assert bundle2.log_entry["leaf_index"] == 2


# ---------------------------------------------------------------------------
# Edge case coverage for verify()
# ---------------------------------------------------------------------------

class TestVerifyEdgeCases:
    def _write_bundle(self, capsule_dir, bundle: SealBundle) -> None:
        seal_dir = capsule_dir / ".seal"
        seal_dir.mkdir(exist_ok=True)
        (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
        (seal_dir / "manifest.dsse.tsr").write_bytes(bundle.tsr)
        (seal_dir / "log-entry.json").write_text(
            json.dumps(bundle.log_entry, indent=2), encoding="utf-8"
        )

    def test_tsr_present_empty_ok(self, nova_seal, tmp_path):
        """Empty TSR bytes (TSA skipped) → timestamp_ok=True."""
        manifest = {"run_id": "tsr-empty"}
        bundle = nova_seal.seal(manifest)
        capsule_dir = tmp_path / "cap-tsr-empty"
        capsule_dir.mkdir()
        seal_dir = capsule_dir / ".seal"
        seal_dir.mkdir()
        (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
        (seal_dir / "manifest.dsse.tsr").write_bytes(b"")  # empty TSR
        (seal_dir / "log-entry.json").write_text(
            json.dumps(bundle.log_entry, indent=2), encoding="utf-8"
        )
        result = nova_seal.verify(bundle.capsule_id, str(seal_dir))
        assert result.timestamp_ok is True

    def test_tsr_present_with_valid_bytes(self, signing_materials, tmp_path):
        """Present non-empty TSR that actually matches DSSE hash → timestamp_ok=True."""
        import hashlib

        key_path, cert_path, mat_tmp = signing_materials
        config = KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path))
        seal = NovaSeal(config=config, tsa_url="", db_path=str(tmp_path / "v_tsr.db"))
        manifest = {"run_id": "tsr-valid"}
        bundle = seal.seal(manifest)

        # Build a minimal TSR that contains SHA256(dsse_bytes)
        expected_hash = hashlib.sha256(bundle.dsse_envelope).digest()
        def der_seq(c: bytes) -> bytes: return bytes([0x30, len(c)]) + c
        def der_int(n: int) -> bytes: return bytes([0x02, 0x01, n])
        def der_oct(d: bytes) -> bytes: return bytes([0x04, len(d)]) + d
        tsr = der_seq(der_seq(der_int(0)) + der_oct(expected_hash))

        capsule_dir = tmp_path / "cap-tsr-valid"
        capsule_dir.mkdir()
        seal_dir = capsule_dir / ".seal"
        seal_dir.mkdir()
        (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
        (seal_dir / "manifest.dsse.tsr").write_bytes(tsr)
        (seal_dir / "log-entry.json").write_text(
            json.dumps(bundle.log_entry, indent=2), encoding="utf-8"
        )
        result = seal.verify(bundle.capsule_id, str(seal_dir))
        assert result.timestamp_ok is True

    def test_tsr_present_wrong_hash_fails(self, nova_seal, tmp_path):
        """Present non-empty TSR with wrong hash → timestamp_ok=False."""
        manifest = {"run_id": "tsr-wrong-hash"}
        bundle = nova_seal.seal(manifest)
        capsule_dir = tmp_path / "cap-tsr-bad"
        capsule_dir.mkdir()
        seal_dir = capsule_dir / ".seal"
        seal_dir.mkdir()
        (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
        (seal_dir / "manifest.dsse.tsr").write_bytes(b"\x30\x03\x02\x01\x00")  # granted, no hash
        (seal_dir / "log-entry.json").write_text(
            json.dumps(bundle.log_entry, indent=2), encoding="utf-8"
        )
        result = nova_seal.verify(bundle.capsule_id, str(seal_dir))
        assert result.timestamp_ok is False

    def test_missing_log_entry_file_fails(self, nova_seal, tmp_path):
        manifest = {"run_id": "no-log-entry"}
        bundle = nova_seal.seal(manifest)
        capsule_dir = tmp_path / "cap-no-log"
        capsule_dir.mkdir()
        seal_dir = capsule_dir / ".seal"
        seal_dir.mkdir()
        (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
        (seal_dir / "manifest.dsse.tsr").write_bytes(bundle.tsr)
        # Don't write log-entry.json
        result = nova_seal.verify(bundle.capsule_id, str(seal_dir))
        assert result.log_integrity_ok is False

    def test_invalid_log_entry_json_fails(self, nova_seal, tmp_path):
        manifest = {"run_id": "bad-json"}
        bundle = nova_seal.seal(manifest)
        capsule_dir = tmp_path / "cap-bad-json"
        capsule_dir.mkdir()
        seal_dir = capsule_dir / ".seal"
        seal_dir.mkdir()
        (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
        (seal_dir / "manifest.dsse.tsr").write_bytes(bundle.tsr)
        (seal_dir / "log-entry.json").write_bytes(b"not valid json {{{")
        result = nova_seal.verify(bundle.capsule_id, str(seal_dir))
        assert result.log_integrity_ok is False
        assert any("Merkle verification error" in e for e in result.errors)

    def test_log_entry_missing_leaf_index(self, nova_seal, tmp_path):
        manifest = {"run_id": "no-leaf-idx"}
        bundle = nova_seal.seal(manifest)
        capsule_dir = tmp_path / "cap-no-leaf"
        capsule_dir.mkdir()
        seal_dir = capsule_dir / ".seal"
        seal_dir.mkdir()
        (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
        (seal_dir / "manifest.dsse.tsr").write_bytes(bundle.tsr)
        (seal_dir / "log-entry.json").write_text('{"no_leaf": true}', encoding="utf-8")
        result = nova_seal.verify(bundle.capsule_id, str(seal_dir))
        assert result.log_integrity_ok is False
        assert any("leaf_index" in e for e in result.errors)


# ---------------------------------------------------------------------------
# TSA error paths in seal()
# ---------------------------------------------------------------------------

class TestSealTSAPaths:
    def test_tsa_unavailable_does_not_raise(self, signing_materials, tmp_path):
        """TSAUnavailableError is caught and logged; sealing continues."""
        from unittest.mock import patch

        from novafabric.trust.novaseal.timestamp import TSAUnavailableError

        key_path, cert_path, _ = signing_materials
        config = KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path))
        seal = NovaSeal(
            config=config,
            tsa_url="https://down.example.com/tsr",
            db_path=str(tmp_path / "unavail.db"),
        )
        with patch(
            "novafabric.trust.novaseal.request_timestamp",
            side_effect=TSAUnavailableError("down"),
        ):
            bundle = seal.seal({"run_id": "tsa-down"})
        assert bundle.tsr == b""

    def test_generic_tsa_error_does_not_raise(self, signing_materials, tmp_path):
        """Generic TSA exception is caught and logged; sealing continues."""
        from unittest.mock import patch

        key_path, cert_path, _ = signing_materials
        config = KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path))
        seal = NovaSeal(
            config=config,
            tsa_url="https://error.example.com/tsr",
            db_path=str(tmp_path / "generic.db"),
        )
        with patch(
            "novafabric.trust.novaseal.request_timestamp",
            side_effect=RuntimeError("unexpected"),
        ):
            bundle = seal.seal({"run_id": "tsa-generic"})
        assert bundle.tsr == b""
