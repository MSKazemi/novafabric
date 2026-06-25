"""Tests for NovaSeal config loading (novafabric.trust.novaseal.config)."""

from __future__ import annotations

import datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from novafabric.trust.novaseal.config import (
    _DEFAULT_TSA_URL,
    SealConfigError,
    SigningProfile,
    _parse_profile,
    load_signing_profile,
)

# ---------------------------------------------------------------------------
# Fixture: valid novaseal.yaml content + key/cert on disk
# ---------------------------------------------------------------------------

@pytest.fixture()
def key_and_cert(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key_path = tmp_path / "seal.key"
    key_path.write_bytes(key_pem)

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "NovaSeal-Config-Test")])
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
    return key_path, cert_path


@pytest.fixture()
def valid_yaml(tmp_path, key_and_cert):
    key_path, cert_path = key_and_cert
    cfg = tmp_path / "novaseal.yaml"
    cfg.write_text(
        f"profile: local\n"
        f"key_path: {key_path}\n"
        f"cert_path: {cert_path}\n"
        f"tsa_url: https://test.example.com/tsr\n"
        f"merkle_db: {tmp_path / 'merkle.db'}\n"
    )
    return cfg


# ---------------------------------------------------------------------------
# _parse_profile
# ---------------------------------------------------------------------------

class TestParseProfile:
    def test_minimal_valid(self, tmp_path, key_and_cert):
        key_path, cert_path = key_and_cert
        cfg = tmp_path / "novaseal.yaml"
        cfg.write_text(f"profile: local\nkey_path: {key_path}\ncert_path: {cert_path}\n")
        profile = _parse_profile(cfg)
        assert isinstance(profile, SigningProfile)
        assert profile.profile == "local"
        assert profile.tsa_url == _DEFAULT_TSA_URL

    def test_full_yaml(self, tmp_path, valid_yaml):
        profile = _parse_profile(valid_yaml)
        assert profile.tsa_url == "https://test.example.com/tsr"

    def test_unsupported_profile_raises(self, tmp_path, key_and_cert):
        key_path, cert_path = key_and_cert
        cfg = tmp_path / "novaseal.yaml"
        cfg.write_text(
            f"profile: sigstore\nkey_path: {key_path}\ncert_path: {cert_path}\n"
        )
        with pytest.raises(SealConfigError, match="not supported"):
            _parse_profile(cfg)

    def test_missing_key_path_raises(self, tmp_path, key_and_cert):
        _, cert_path = key_and_cert
        cfg = tmp_path / "novaseal.yaml"
        cfg.write_text(f"profile: local\ncert_path: {cert_path}\n")
        with pytest.raises(SealConfigError, match="key_path"):
            _parse_profile(cfg)

    def test_missing_cert_path_raises(self, tmp_path, key_and_cert):
        key_path, _ = key_and_cert
        cfg = tmp_path / "novaseal.yaml"
        cfg.write_text(f"profile: local\nkey_path: {key_path}\n")
        with pytest.raises(SealConfigError, match="cert_path"):
            _parse_profile(cfg)

    def test_nonexistent_key_raises(self, tmp_path, key_and_cert):
        _, cert_path = key_and_cert
        cfg = tmp_path / "novaseal.yaml"
        cfg.write_text(
            f"profile: local\nkey_path: /no/such/key.pem\ncert_path: {cert_path}\n"
        )
        with pytest.raises(SealConfigError, match="key_path not found"):
            _parse_profile(cfg)

    def test_nonexistent_cert_raises(self, tmp_path, key_and_cert):
        key_path, _ = key_and_cert
        cfg = tmp_path / "novaseal.yaml"
        cfg.write_text(
            f"profile: local\nkey_path: {key_path}\ncert_path: /no/such/cert.pem\n"
        )
        with pytest.raises(SealConfigError, match="cert_path not found"):
            _parse_profile(cfg)

    def test_non_mapping_raises(self, tmp_path):
        cfg = tmp_path / "novaseal.yaml"
        cfg.write_text("- not a mapping\n")
        with pytest.raises(SealConfigError, match="YAML mapping"):
            _parse_profile(cfg)

    def test_merkle_db_defaults(self, tmp_path, key_and_cert):
        key_path, cert_path = key_and_cert
        cfg = tmp_path / "novaseal.yaml"
        cfg.write_text(f"profile: local\nkey_path: {key_path}\ncert_path: {cert_path}\n")
        profile = _parse_profile(cfg)
        # Merkle DB defaults to ~/.novafabric/novaseal-merkle.db
        assert "novaseal-merkle" in str(profile.merkle_db)


# ---------------------------------------------------------------------------
# load_signing_profile
# ---------------------------------------------------------------------------

class TestLoadSigningProfile:
    def test_returns_none_when_no_config(self, monkeypatch, tmp_path):
        monkeypatch.delenv("NOVAFABRIC_SEAL_CONFIG", raising=False)
        # Point default config path to non-existent location
        monkeypatch.setattr(
            "novafabric.trust.novaseal.config._DEFAULT_CONFIG_PATH",
            tmp_path / "nonexistent.yaml",
        )
        assert load_signing_profile() is None

    def test_reads_from_env_var(self, monkeypatch, valid_yaml):
        monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(valid_yaml))
        profile = load_signing_profile()
        assert profile is not None
        assert profile.profile == "local"

    def test_env_missing_file_raises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(tmp_path / "missing.yaml"))
        with pytest.raises(SealConfigError, match="missing file"):
            load_signing_profile()

    def test_reads_default_path(self, monkeypatch, valid_yaml, tmp_path):
        monkeypatch.delenv("NOVAFABRIC_SEAL_CONFIG", raising=False)
        monkeypatch.setattr(
            "novafabric.trust.novaseal.config._DEFAULT_CONFIG_PATH",
            valid_yaml,
        )
        profile = load_signing_profile()
        assert profile is not None
        assert profile.tsa_url == "https://test.example.com/tsr"
