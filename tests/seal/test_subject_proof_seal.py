"""Tests for NovaSeal-signed nova subject-proof output (G-CROSS-004 / FR-03).

Verifies:
- subject_proof_cmd produces both .json and .seal.json when NovaSeal is configured.
- The .seal.json is a valid DSSE envelope with intent=verified.
- When NovaSeal is NOT configured, only the .json is written (no error).
- nova verify --check-redaction passes on a valid seal, fails on a tampered one.
"""

from __future__ import annotations

import datetime
import json
import os

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from novafabric.trust.novaseal.envelope import SigningIntent, extract_intent, verify_envelope

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def novaseal_config(tmp_path):
    """Write a novaseal.yaml config and backing key/cert; return config path."""
    key = ec.generate_private_key(ec.SECP256R1())
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key_path = tmp_path / "seal.key"
    key_path.write_bytes(key_pem)

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SubjectProofTest")])
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

    merkle_db = tmp_path / "merkle.db"
    config_path = tmp_path / "novaseal.yaml"
    config_path.write_text(
        f"profile: local\n"
        f"key_path: {key_path}\n"
        f"cert_path: {cert_path}\n"
        f"tsa_url: \n"
        f"merkle_db: {merkle_db}\n"
    )
    return config_path


@pytest.fixture()
def pepper_env(monkeypatch):
    monkeypatch.setenv("NOVA_PII_PEPPER", "test-pepper-value-for-unit-tests")


# ---------------------------------------------------------------------------
# _try_seal_proof_report — unit tests for the helper
# ---------------------------------------------------------------------------


class TestTrySealProofReport:
    def test_produces_seal_file_when_novaseal_configured(self, novaseal_config, tmp_path, monkeypatch):
        """With a valid config, .seal.json is written alongside the report."""
        from novafabric.cli.redact import _try_seal_proof_report

        report_path = tmp_path / "redaction_proof_report.json"
        proof_bytes = b'{"subject_id_hmac": "sha256:abc", "records": []}'
        report_path.write_bytes(proof_bytes)

        monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(novaseal_config))
        _try_seal_proof_report(report_path, proof_bytes)

        seal_path = tmp_path / "redaction_proof_report.seal.json"
        assert seal_path.exists(), ".seal.json was not created"
        assert seal_path.stat().st_size > 0

    def test_seal_file_is_valid_dsse_envelope(self, novaseal_config, tmp_path, monkeypatch):
        """The .seal.json is a valid DSSE envelope that verifies correctly."""
        from novafabric.cli.redact import _try_seal_proof_report

        report_path = tmp_path / "redaction_proof_report.json"
        proof_bytes = b'{"subject_id_hmac": "sha256:def", "records": []}'
        report_path.write_bytes(proof_bytes)

        monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(novaseal_config))
        _try_seal_proof_report(report_path, proof_bytes)

        seal_path = tmp_path / "redaction_proof_report.seal.json"
        seal_bytes = seal_path.read_bytes()

        # Must verify as a valid DSSE envelope
        assert verify_envelope(seal_bytes) is True

    def test_seal_file_has_verified_intent(self, novaseal_config, tmp_path, monkeypatch):
        """The .seal.json has signing_intent=VERIFIED (FR-03)."""
        from novafabric.cli.redact import _try_seal_proof_report

        report_path = tmp_path / "redaction_proof_report.json"
        proof_bytes = b'{"subject_id_hmac": "sha256:ghi", "records": []}'
        report_path.write_bytes(proof_bytes)

        monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(novaseal_config))
        _try_seal_proof_report(report_path, proof_bytes)

        seal_path = tmp_path / "redaction_proof_report.seal.json"
        seal_bytes = seal_path.read_bytes()
        intent = extract_intent(seal_bytes)
        assert intent is SigningIntent.VERIFIED

    def test_no_seal_when_novaseal_not_configured(self, tmp_path, monkeypatch):
        """Without NovaSeal config, no .seal.json is written and no error is raised."""
        from novafabric.cli.redact import _try_seal_proof_report

        monkeypatch.delenv("NOVAFABRIC_SEAL_CONFIG", raising=False)
        monkeypatch.setattr(
            "novafabric.trust.novaseal.config._DEFAULT_CONFIG_PATH",
            tmp_path / "nonexistent.yaml",
        )

        report_path = tmp_path / "redaction_proof_report.json"
        proof_bytes = b'{"records": []}'
        report_path.write_bytes(proof_bytes)

        # Must not raise
        _try_seal_proof_report(report_path, proof_bytes)

        seal_path = tmp_path / "redaction_proof_report.seal.json"
        assert not seal_path.exists(), ".seal.json should not be created without config"


# ---------------------------------------------------------------------------
# nova verify --check-redaction
# ---------------------------------------------------------------------------


class TestVerifyCheckRedaction:
    def _make_seal_file(self, tmp_path, novaseal_config) -> tuple:
        """Create a valid .seal.json and return (report_path, seal_path)."""
        from novafabric.cli.redact import _try_seal_proof_report

        os.environ["NOVAFABRIC_SEAL_CONFIG"] = str(novaseal_config)
        report_path = tmp_path / "redaction_proof_report.json"
        proof_bytes = b'{"subject_id_hmac": "sha256:xyz", "records": []}'
        report_path.write_bytes(proof_bytes)
        _try_seal_proof_report(report_path, proof_bytes)
        seal_path = tmp_path / "redaction_proof_report.seal.json"
        return report_path, seal_path

    def test_check_redaction_valid_seal_exits_0(self, novaseal_config, tmp_path, monkeypatch):
        """--check-redaction with a valid seal file exits 0."""
        monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(novaseal_config))

        from novafabric.cli.redact import _try_seal_proof_report

        report_path = tmp_path / "proof.json"
        proof_bytes = b'{"records": []}'
        report_path.write_bytes(proof_bytes)
        _try_seal_proof_report(report_path, proof_bytes)
        seal_path = tmp_path / "proof.seal.json"
        assert seal_path.exists()

        from novafabric.cli.verify import _verify_redaction_seal
        # Must not raise typer.Exit(code=1)
        try:
            _verify_redaction_seal(seal_path)
        except SystemExit as exc:
            assert exc.code == 0 or exc.code is None, f"Expected exit 0, got {exc.code}"

    def test_check_redaction_tampered_seal_exits_1(self, novaseal_config, tmp_path, monkeypatch):
        """--check-redaction with a tampered seal file exits 1."""
        import typer

        monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(novaseal_config))

        from novafabric.cli.redact import _try_seal_proof_report

        report_path = tmp_path / "proof_tampered.json"
        proof_bytes = b'{"records": []}'
        report_path.write_bytes(proof_bytes)
        _try_seal_proof_report(report_path, proof_bytes)
        seal_path = tmp_path / "proof_tampered.seal.json"

        # Tamper with the payload
        env = json.loads(seal_path.read_bytes())
        env["payload"] = "dGFtcGVyZWQ"  # base64url("tampered")
        seal_path.write_bytes(json.dumps(env).encode())

        from novafabric.cli.verify import _verify_redaction_seal

        with pytest.raises(typer.Exit) as exc_info:
            _verify_redaction_seal(seal_path)
        assert exc_info.value.exit_code == 1

    def test_check_redaction_missing_file_exits_1(self, tmp_path):
        """--check-redaction with a non-existent file exits 1."""
        import typer

        from novafabric.cli.verify import _verify_redaction_seal

        with pytest.raises(typer.Exit) as exc_info:
            _verify_redaction_seal(tmp_path / "nonexistent.seal.json")
        assert exc_info.value.exit_code == 1
