"""Tests for the `nova verify` CLI command."""

from __future__ import annotations

import datetime
import json

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.trust.novaseal import KeyConfig, NovaSeal

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixture: NovaSeal instance + a written capsule with .seal/
# ---------------------------------------------------------------------------

@pytest.fixture()
def sealed_capsule(tmp_path):
    """Create a capsule dir with a valid .seal/ bundle. Returns (capsule_dir, config_path)."""
    key = ec.generate_private_key(ec.SECP256R1())
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key_path = tmp_path / "seal.key"
    key_path.write_bytes(key_pem)

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "NovaSeal-CLI-Test")])
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

    config = KeyConfig(
        profile="local",
        key_path=str(key_path),
        cert_path=str(cert_path),
    )
    seal = NovaSeal(config=config, tsa_url="", db_path=str(merkle_db))

    capsule_dir = tmp_path / "test-capsule-abc"
    capsule_dir.mkdir()
    manifest = {"run_id": "cli-test-001", "status": "success"}
    bundle = seal.seal(manifest)

    seal_dir = capsule_dir / ".seal"
    seal_dir.mkdir()
    (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal_dir / "manifest.dsse.tsr").write_bytes(bundle.tsr)
    (seal_dir / "log-entry.json").write_text(
        json.dumps(bundle.log_entry, indent=2), encoding="utf-8"
    )

    return capsule_dir, config_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestVerifyCommand:
    def test_verify_success(self, sealed_capsule):
        capsule_dir, config_path = sealed_capsule
        result = runner.invoke(
            app,
            ["verify", str(capsule_dir), "--seal-config", str(config_path)],
        )
        assert result.exit_code == 0, result.output
        assert "signature_ok=True" in result.output
        assert "timestamp_ok=True" in result.output
        assert "log_integrity_ok=True" in result.output

    def test_verify_missing_capsule_dir_exits_1(self, tmp_path):
        result = runner.invoke(app, ["verify", str(tmp_path / "nonexistent")])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_verify_no_seal_dir_exits_1(self, tmp_path):
        capsule_dir = tmp_path / "no-seal"
        capsule_dir.mkdir()
        result = runner.invoke(app, ["verify", str(capsule_dir)])
        assert result.exit_code == 1
        assert ".seal" in result.output

    def test_verify_no_config_exits_1(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NOVAFABRIC_SEAL_CONFIG", raising=False)
        capsule_dir = tmp_path / "cap"
        capsule_dir.mkdir()
        seal_dir = capsule_dir / ".seal"
        seal_dir.mkdir()
        # Patch default config path to non-existent
        monkeypatch.setattr(
            "novafabric.trust.novaseal.config._DEFAULT_CONFIG_PATH",
            tmp_path / "nonexistent.yaml",
        )
        result = runner.invoke(app, ["verify", str(capsule_dir)])
        assert result.exit_code == 1
        assert "not configured" in result.output.lower()

    def test_verify_shows_command_in_help(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "verify" in result.output

    def test_verify_tampered_dsse_exits_1(self, sealed_capsule):
        capsule_dir, config_path = sealed_capsule
        # Tamper with the DSSE file
        dsse_path = capsule_dir / ".seal" / "manifest.dsse"
        original = json.loads(dsse_path.read_bytes())
        original["payload"] = "dGFtcGVyZWQ"
        dsse_path.write_bytes(json.dumps(original).encode())

        result = runner.invoke(
            app,
            ["verify", str(capsule_dir), "--seal-config", str(config_path)],
        )
        assert result.exit_code == 1
        assert "FAIL" in result.output


def _local_config(base):
    """Rebuild the fixture's local KeyConfig from the paths it wrote."""
    return KeyConfig(
        profile="local",
        key_path=str(base / "seal.key"),
        cert_path=str(base / "seal.crt"),
    )


class TestTimestampPresenceIsReportedHonestly:
    """``nova verify`` must not print "Timestamp: OK" for a capsule with no token.

    Timestamping is best-effort: an absent or empty ``manifest.dsse.tsr`` still
    verifies, so ``timestamp_ok`` is True in that case.  On its own it therefore
    cannot distinguish "timestamped and verified" from "never timestamped" — and
    the CLI printed a green ✓ either way, claiming evidence the capsule does not
    carry.  ``timestamp_present`` is what separates the two.
    """

    def test_result_marks_an_empty_tsr_as_not_present(self, sealed_capsule):
        capsule_dir, config_path = sealed_capsule
        tsr = capsule_dir / ".seal" / "manifest.dsse.tsr"
        assert tsr.read_bytes() == b"", "fixture expects a TSA-skipped capsule"

        seal = NovaSeal(
            config=_local_config(config_path.parent),
            tsa_url="",
            db_path=str(config_path.parent / "merkle.db"),
        )
        result = seal.verify("", str(capsule_dir / ".seal"))

        assert result.timestamp_ok is True  # best-effort: absence is not failure
        assert result.timestamp_present is False

    def test_result_marks_a_missing_tsr_file_as_not_present(self, sealed_capsule):
        capsule_dir, config_path = sealed_capsule
        (capsule_dir / ".seal" / "manifest.dsse.tsr").unlink()

        seal = NovaSeal(
            config=_local_config(config_path.parent),
            tsa_url="",
            db_path=str(config_path.parent / "merkle.db"),
        )
        result = seal.verify("", str(capsule_dir / ".seal"))

        assert result.timestamp_ok is True
        assert result.timestamp_present is False

    def test_cli_prints_not_present_instead_of_ok(self, sealed_capsule):
        capsule_dir, config_path = sealed_capsule
        result = runner.invoke(
            app,
            ["verify", str(capsule_dir), "--seal-config", str(config_path)],
        )
        assert result.exit_code == 0, result.output
        assert "NOT PRESENT" in result.output
        # The machine-readable line still reports the best-effort verdict.
        assert "timestamp_ok=True" in result.output

    def test_a_real_token_is_marked_present_and_checked(
        self, sealed_capsule, monkeypatch
    ):
        """The positive case: a non-empty TSR must be verified *and* flagged.

        Without this, ``timestamp_present`` would be satisfied by a constant
        ``False`` — the very bug the absent-token tests above cannot catch.
        """
        capsule_dir, config_path = sealed_capsule
        (capsule_dir / ".seal" / "manifest.dsse.tsr").write_bytes(b"\x30\x80fake-der")

        seen: list[bytes] = []

        def _fake_verify_timestamp(tsr_bytes, dsse_bytes):
            seen.append(tsr_bytes)
            return True

        monkeypatch.setattr(
            "novafabric.trust.novaseal.verify_timestamp", _fake_verify_timestamp
        )
        seal = NovaSeal(
            config=_local_config(config_path.parent),
            tsa_url="",
            db_path=str(config_path.parent / "merkle.db"),
        )
        result = seal.verify("", str(capsule_dir / ".seal"))

        assert seen == [b"\x30\x80fake-der"]
        assert result.timestamp_present is True
        assert result.timestamp_ok is True

        cli = runner.invoke(
            app, ["verify", str(capsule_dir), "--seal-config", str(config_path)]
        )
        assert "NOT PRESENT" not in cli.output
