"""Tests for Cloud KMS signing backends and config integration (B-7).

Covers:
- LocalSigningBackend sign/cert round-trip
- AwsKmsSigningBackend (mocked boto3)
- AzureKvSigningBackend ImportError
- GcpKmsSigningBackend ImportError
- build_signing_backend() dispatch
- _parse_profile() for aws_kms / azure_kv / gcp_kms profiles
- create_envelope() with a LocalSigningBackend backend kwarg
- Backward-compatibility of existing profile: local yaml
"""

from __future__ import annotations

import datetime
import hashlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ec_key_and_cert(tmp_path_factory):
    """Generate a P-256 key pair and self-signed cert, return (key_path, cert_path, key)."""
    tmp = tmp_path_factory.mktemp("kms_keys")
    key = ec.generate_private_key(ec.SECP256R1())
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key_path = tmp / "seal.key"
    key_path.write_bytes(key_pem)

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "NovaSeal-KMS-Test")])
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
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    cert_path = tmp / "seal.crt"
    cert_path.write_bytes(cert_pem)

    return key_path, cert_path, key


# ---------------------------------------------------------------------------
# LocalSigningBackend
# ---------------------------------------------------------------------------

class TestLocalSigningBackend:
    def test_sign_digest_returns_valid_ecdsa_signature(self, ec_key_and_cert):
        """sign_digest() produces a DER ECDSA P-256 signature verifiable with cryptography."""
        from cryptography.hazmat.primitives.asymmetric.ec import ECDSA
        from cryptography.hazmat.primitives.asymmetric.utils import Prehashed
        from cryptography.hazmat.primitives.hashes import SHA256

        from novafabric.trust.novaseal.signing_backend import LocalSigningBackend

        key_path, cert_path, key = ec_key_and_cert
        backend = LocalSigningBackend(key_path, cert_path)

        import hashlib

        digest = hashlib.sha256(b"test payload").digest()
        sig = backend.sign_digest(digest)

        # Signature must be bytes
        assert isinstance(sig, bytes)
        assert len(sig) > 0

        # Verify with the public key
        pub = key.public_key()
        pub.verify(sig, digest, ECDSA(Prehashed(SHA256())))  # raises on failure

    def test_get_cert_der_returns_der_bytes(self, ec_key_and_cert):
        """get_cert_der() returns DER-encoded certificate bytes."""
        from cryptography.x509 import load_der_x509_certificate

        from novafabric.trust.novaseal.signing_backend import LocalSigningBackend

        key_path, cert_path, _ = ec_key_and_cert
        backend = LocalSigningBackend(key_path, cert_path)
        der = backend.get_cert_der()

        assert isinstance(der, bytes)
        assert len(der) > 0
        # Must be parseable as a DER X.509 certificate
        cert = load_der_x509_certificate(der)
        assert cert.subject is not None

    def test_sign_digest_different_digests_give_different_signatures(self, ec_key_and_cert):
        """Each sign_digest() call with a different digest produces different output."""
        import hashlib

        from novafabric.trust.novaseal.signing_backend import LocalSigningBackend

        key_path, cert_path, _ = ec_key_and_cert
        backend = LocalSigningBackend(key_path, cert_path)

        d1 = hashlib.sha256(b"payload one").digest()
        d2 = hashlib.sha256(b"payload two").digest()
        sig1 = backend.sign_digest(d1)
        sig2 = backend.sign_digest(d2)
        # Different digests → (almost certainly) different signatures
        assert sig1 != sig2


# ---------------------------------------------------------------------------
# AwsKmsSigningBackend — lazy import and mocked signing
# ---------------------------------------------------------------------------

class TestAwsKmsSigningBackend:
    def test_raises_importerror_when_boto3_missing(self, ec_key_and_cert, monkeypatch):
        """sign_digest() raises ImportError with install hint when boto3 is absent."""
        from novafabric.trust.novaseal.signing_backend import AwsKmsSigningBackend

        key_path, cert_path, _ = ec_key_and_cert
        backend = AwsKmsSigningBackend(
            key_id="arn:aws:kms:us-east-1:123456789012:key/test-key",
            cert_path=cert_path,
        )

        # Hide boto3 so the lazy import fails
        monkeypatch.setitem(sys.modules, "boto3", None)  # type: ignore[arg-type]
        # Remove cached client so it re-imports
        backend._client = None

        with pytest.raises(ImportError, match="pip install novafabric\\[seal-aws\\]"):
            backend.sign_digest(b"\x00" * 32)

    def test_sign_digest_uses_kms_sign(self, ec_key_and_cert):
        """sign_digest() calls kms.sign with correct parameters and returns signature bytes."""
        from novafabric.trust.novaseal.signing_backend import AwsKmsSigningBackend

        key_path, cert_path, _ = ec_key_and_cert
        fake_sig = b"\xDE\xAD\xBE\xEF" * 16  # 64-byte fake DER sig
        mock_kms = MagicMock()
        mock_kms.sign.return_value = {"Signature": fake_sig}

        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_kms

        backend = AwsKmsSigningBackend(
            key_id="arn:aws:kms:us-east-1:123456789012:key/test-key",
            cert_path=cert_path,
            region="eu-west-1",
        )
        backend._client = mock_kms  # inject pre-built mock

        digest = b"\xaa" * 32
        result = backend.sign_digest(digest)

        mock_kms.sign.assert_called_once_with(
            KeyId="arn:aws:kms:us-east-1:123456789012:key/test-key",
            Message=digest,
            MessageType="DIGEST",
            SigningAlgorithm="ECDSA_SHA_256",
        )
        assert result == fake_sig

    def test_get_cert_der_returns_der(self, ec_key_and_cert):
        """AwsKmsSigningBackend.get_cert_der() reads from the local cert file."""
        from cryptography.x509 import load_der_x509_certificate

        from novafabric.trust.novaseal.signing_backend import AwsKmsSigningBackend

        _, cert_path, _ = ec_key_and_cert
        backend = AwsKmsSigningBackend(
            key_id="arn:aws:kms:us-east-1:123456789012:key/k",
            cert_path=cert_path,
        )
        der = backend.get_cert_der()
        cert = load_der_x509_certificate(der)
        assert "NovaSeal" in cert.subject.rfc4514_string()


# ---------------------------------------------------------------------------
# AzureKvSigningBackend — lazy import check
# ---------------------------------------------------------------------------

class TestAzureKvSigningBackend:
    def test_raises_importerror_when_azure_sdk_missing(self, ec_key_and_cert, monkeypatch):
        """sign_digest() raises ImportError with install hint when azure SDK is absent."""
        from novafabric.trust.novaseal.signing_backend import AzureKvSigningBackend

        _, cert_path, _ = ec_key_and_cert
        backend = AzureKvSigningBackend(
            vault_url="https://myvault.vault.azure.net/",
            key_name="test-key",
            cert_path=cert_path,
        )

        # Forcibly block the azure imports
        monkeypatch.setitem(sys.modules, "azure.identity", None)  # type: ignore[arg-type]
        monkeypatch.setitem(sys.modules, "azure.keyvault.keys", None)  # type: ignore[arg-type]
        monkeypatch.setitem(sys.modules, "azure.keyvault.keys.crypto", None)  # type: ignore[arg-type]

        with pytest.raises(ImportError, match="pip install novafabric\\[seal-azure\\]"):
            backend.sign_digest(b"\x00" * 32)

    def test_converts_p1363_signature_to_der(self, ec_key_and_cert, monkeypatch):
        """Key Vault returns raw r||s; sign_digest() must return DER.

        Regression for a bug found 2026-08-27 against a LIVE vault: Key Vault's
        es256 returns a 64-byte IEEE-P1363 signature, unlike AWS KMS, GCP KMS and
        the local PEM backend, which all return DER. The raw bytes were passed
        through unchanged, so create_envelope() succeeded and verify_envelope()
        then failed with "signature mismatch" — every Azure-sealed capsule was
        unverifiable, and no test caught it because every Azure test was a mock.
        """
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec as _ec
        from cryptography.hazmat.primitives.asymmetric import utils as _asym

        from novafabric.trust.novaseal.signing_backend import AzureKvSigningBackend

        _, cert_path, key = ec_key_and_cert
        digest = hashlib.sha256(b"payload").digest()

        # Sign locally, then re-encode as raw r||s to mimic what Key Vault returns.
        der_sig = key.sign(digest, _ec.ECDSA(_asym.Prehashed(hashes.SHA256())))
        r, s_val = _asym.decode_dss_signature(der_sig)
        p1363 = r.to_bytes(32, "big") + s_val.to_bytes(32, "big")
        assert len(p1363) == 64 and p1363[:1] != b"\x30"

        pub_numbers = key.public_key().public_numbers()

        class _FakeCrypto:
            def __init__(self, *a, **kw): pass
            def sign(self, alg, dg):
                return SimpleNamespace(signature=p1363)

        class _FakeKeyClient:
            def __init__(self, *a, **kw): pass
            def get_key(self, name): return SimpleNamespace(key=pub_numbers)

        monkeypatch.setitem(
            sys.modules, "azure.identity",
            SimpleNamespace(DefaultAzureCredential=lambda *a, **kw: object()),
        )
        monkeypatch.setitem(
            sys.modules, "azure.keyvault.keys", SimpleNamespace(KeyClient=_FakeKeyClient),
        )
        monkeypatch.setitem(
            sys.modules, "azure.keyvault.keys.crypto",
            SimpleNamespace(
                CryptographyClient=_FakeCrypto,
                SignatureAlgorithm=SimpleNamespace(es256="ES256"),
            ),
        )

        backend = AzureKvSigningBackend(
            vault_url="https://myvault.vault.azure.net/",
            key_name="test-key",
            cert_path=cert_path,
        )
        out = backend.sign_digest(digest)

        # Must be DER, and must verify as DER against the real public key.
        assert out[:1] == b"\x30", "sign_digest() must return DER, not raw r||s"
        key.public_key().verify(
            out, digest, _ec.ECDSA(_asym.Prehashed(hashes.SHA256()))
        )

    def test_passes_der_signature_through_unchanged(self, ec_key_and_cert, monkeypatch):
        """A backend already returning DER must not be re-encoded."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec as _ec
        from cryptography.hazmat.primitives.asymmetric import utils as _asym

        from novafabric.trust.novaseal.signing_backend import AzureKvSigningBackend

        _, cert_path, key = ec_key_and_cert
        digest = hashlib.sha256(b"payload").digest()
        der_sig = key.sign(digest, _ec.ECDSA(_asym.Prehashed(hashes.SHA256())))

        class _FakeCrypto:
            def __init__(self, *a, **kw): pass
            def sign(self, alg, dg): return SimpleNamespace(signature=der_sig)

        class _FakeKeyClient:
            def __init__(self, *a, **kw): pass
            def get_key(self, name): return SimpleNamespace(key=None)

        monkeypatch.setitem(
            sys.modules, "azure.identity",
            SimpleNamespace(DefaultAzureCredential=lambda *a, **kw: object()),
        )
        monkeypatch.setitem(
            sys.modules, "azure.keyvault.keys", SimpleNamespace(KeyClient=_FakeKeyClient),
        )
        monkeypatch.setitem(
            sys.modules, "azure.keyvault.keys.crypto",
            SimpleNamespace(
                CryptographyClient=_FakeCrypto,
                SignatureAlgorithm=SimpleNamespace(es256="ES256"),
            ),
        )

        backend = AzureKvSigningBackend(
            vault_url="https://myvault.vault.azure.net/",
            key_name="test-key",
            cert_path=cert_path,
        )
        assert backend.sign_digest(digest) == der_sig


# ---------------------------------------------------------------------------
# GcpKmsSigningBackend — lazy import check
# ---------------------------------------------------------------------------

class TestGcpKmsSigningBackend:
    def test_raises_importerror_when_gcp_sdk_missing(self, ec_key_and_cert, monkeypatch):
        """sign_digest() raises ImportError with install hint when google-cloud-kms absent."""
        from novafabric.trust.novaseal.signing_backend import GcpKmsSigningBackend

        _, cert_path, _ = ec_key_and_cert
        backend = GcpKmsSigningBackend(
            key_version_name=(
                "projects/p/locations/global/keyRings/r/cryptoKeys/k/cryptoKeyVersions/1"
            ),
            cert_path=cert_path,
        )

        monkeypatch.setitem(sys.modules, "google.cloud", None)  # type: ignore[arg-type]
        monkeypatch.setitem(sys.modules, "google.cloud.kms", None)  # type: ignore[arg-type]

        with pytest.raises(ImportError, match="pip install novafabric\\[seal-gcp\\]"):
            backend.sign_digest(b"\x00" * 32)


# ---------------------------------------------------------------------------
# build_signing_backend()
# ---------------------------------------------------------------------------

class TestBuildSigningBackend:
    def test_returns_local_backend_for_local_profile(self, ec_key_and_cert):
        """build_signing_backend() returns a LocalSigningBackend for profile='local'."""
        from novafabric.trust.novaseal.config import SigningProfile, build_signing_backend
        from novafabric.trust.novaseal.signing_backend import LocalSigningBackend

        key_path, cert_path, _ = ec_key_and_cert
        profile = SigningProfile(
            profile="local",
            key_path=key_path,
            cert_path=cert_path,
        )
        backend = build_signing_backend(profile)
        assert isinstance(backend, LocalSigningBackend)

    def test_returns_aws_backend_for_aws_kms_profile(self, ec_key_and_cert):
        """build_signing_backend() returns AwsKmsSigningBackend for profile='aws_kms'."""
        from novafabric.trust.novaseal.config import SigningProfile, build_signing_backend
        from novafabric.trust.novaseal.signing_backend import AwsKmsSigningBackend

        _, cert_path, _ = ec_key_and_cert
        profile = SigningProfile(
            profile="aws_kms",
            cert_path=cert_path,
            kms_key_id="arn:aws:kms:us-east-1:123456789012:key/test",
            aws_region="us-east-1",
        )
        backend = build_signing_backend(profile)
        assert isinstance(backend, AwsKmsSigningBackend)

    def test_returns_azure_backend_for_azure_kv_profile(self, ec_key_and_cert):
        """build_signing_backend() returns AzureKvSigningBackend for profile='azure_kv'."""
        from novafabric.trust.novaseal.config import SigningProfile, build_signing_backend
        from novafabric.trust.novaseal.signing_backend import AzureKvSigningBackend

        _, cert_path, _ = ec_key_and_cert
        profile = SigningProfile(
            profile="azure_kv",
            cert_path=cert_path,
            vault_url="https://vault.azure.net/",
            key_name="mykey",
        )
        backend = build_signing_backend(profile)
        assert isinstance(backend, AzureKvSigningBackend)

    def test_returns_gcp_backend_for_gcp_kms_profile(self, ec_key_and_cert):
        """build_signing_backend() returns GcpKmsSigningBackend for profile='gcp_kms'."""
        from novafabric.trust.novaseal.config import SigningProfile, build_signing_backend
        from novafabric.trust.novaseal.signing_backend import GcpKmsSigningBackend

        _, cert_path, _ = ec_key_and_cert
        profile = SigningProfile(
            profile="gcp_kms",
            cert_path=cert_path,
            key_version_name=(
                "projects/p/locations/global/keyRings/r/cryptoKeys/k/cryptoKeyVersions/1"
            ),
        )
        backend = build_signing_backend(profile)
        assert isinstance(backend, GcpKmsSigningBackend)


# ---------------------------------------------------------------------------
# _parse_profile() — cloud profiles
# ---------------------------------------------------------------------------

class TestParseProfileCloudProfiles:
    def test_aws_kms_profile_accepted(self, ec_key_and_cert, tmp_path):
        """_parse_profile() accepts profile: aws_kms with required fields."""
        from novafabric.trust.novaseal.config import SigningProfile, _parse_profile

        _, cert_path, _ = ec_key_and_cert
        cfg = tmp_path / "novaseal.yaml"
        cfg.write_text(
            f"profile: aws_kms\n"
            f"kms_key_id: arn:aws:kms:us-east-1:123456789012:key/test\n"
            f"cert_path: {cert_path}\n"
        )
        profile = _parse_profile(cfg)
        assert isinstance(profile, SigningProfile)
        assert profile.profile == "aws_kms"
        assert profile.kms_key_id == "arn:aws:kms:us-east-1:123456789012:key/test"
        assert profile.aws_region == "us-east-1"  # default

    def test_aws_kms_missing_kms_key_id_raises(self, ec_key_and_cert, tmp_path):
        """_parse_profile() raises SealConfigError for aws_kms without kms_key_id."""
        from novafabric.trust.novaseal.config import SealConfigError, _parse_profile

        _, cert_path, _ = ec_key_and_cert
        cfg = tmp_path / "novaseal.yaml"
        cfg.write_text(f"profile: aws_kms\ncert_path: {cert_path}\n")
        with pytest.raises(SealConfigError, match="kms_key_id"):
            _parse_profile(cfg)

    def test_azure_kv_profile_accepted(self, ec_key_and_cert, tmp_path):
        """_parse_profile() accepts profile: azure_kv with required fields."""
        from novafabric.trust.novaseal.config import SigningProfile, _parse_profile

        _, cert_path, _ = ec_key_and_cert
        cfg = tmp_path / "novaseal.yaml"
        cfg.write_text(
            f"profile: azure_kv\n"
            f"vault_url: https://myvault.vault.azure.net/\n"
            f"key_name: my-ec-key\n"
            f"cert_path: {cert_path}\n"
        )
        profile = _parse_profile(cfg)
        assert isinstance(profile, SigningProfile)
        assert profile.profile == "azure_kv"
        assert profile.vault_url == "https://myvault.vault.azure.net/"
        assert profile.key_name == "my-ec-key"

    def test_gcp_kms_profile_accepted(self, ec_key_and_cert, tmp_path):
        """_parse_profile() accepts profile: gcp_kms with required fields."""
        from novafabric.trust.novaseal.config import SigningProfile, _parse_profile

        _, cert_path, _ = ec_key_and_cert
        cfg = tmp_path / "novaseal.yaml"
        cfg.write_text(
            f"profile: gcp_kms\n"
            f"key_version_name: projects/p/locations/global/keyRings/r/cryptoKeys/k/cryptoKeyVersions/1\n"
            f"cert_path: {cert_path}\n"
        )
        profile = _parse_profile(cfg)
        assert isinstance(profile, SigningProfile)
        assert profile.profile == "gcp_kms"
        assert profile.key_version_name.startswith("projects/p/")  # type: ignore[union-attr]

    def test_unsupported_profile_raises(self, ec_key_and_cert, tmp_path):
        """_parse_profile() raises SealConfigError for unknown profile values."""
        from novafabric.trust.novaseal.config import SealConfigError, _parse_profile

        _, cert_path, _ = ec_key_and_cert
        cfg = tmp_path / "novaseal.yaml"
        cfg.write_text(f"profile: sigstore\ncert_path: {cert_path}\n")
        with pytest.raises(SealConfigError, match="not supported"):
            _parse_profile(cfg)


# ---------------------------------------------------------------------------
# create_envelope() with backend kwarg
# ---------------------------------------------------------------------------

class TestCreateEnvelopeWithBackend:
    def test_round_trip_with_local_signing_backend(self, ec_key_and_cert, tmp_path):
        """create_envelope(backend=...) produces a verifiable DSSE envelope."""
        from novafabric.trust.novaseal.envelope import create_envelope, verify_envelope
        from novafabric.trust.novaseal.signing_backend import LocalSigningBackend

        key_path, cert_path, _ = ec_key_and_cert
        backend = LocalSigningBackend(key_path, cert_path)

        payload = b'{"capsule_id": "test-123"}'
        # Pass dummy paths (they must exist for type safety); backend overrides them
        envelope_bytes = create_envelope(
            payload, key_path, cert_path, backend=backend
        )

        assert isinstance(envelope_bytes, bytes)
        import json
        env = json.loads(envelope_bytes)
        assert "signatures" in env
        assert env["payloadType"] == "application/vnd.novafabric.capsule+json"

        # Verify the envelope (should pass end-to-end)
        assert verify_envelope(envelope_bytes)

    def test_backend_kwarg_overrides_key_path(self, ec_key_and_cert, tmp_path):
        """When backend is supplied, key_path/cert_path are not read from disk."""
        from novafabric.trust.novaseal.envelope import create_envelope
        from novafabric.trust.novaseal.signing_backend import LocalSigningBackend

        key_path, cert_path, _ = ec_key_and_cert
        backend = LocalSigningBackend(key_path, cert_path)

        # Pass non-existent dummy paths — should not raise because backend is used
        dummy = tmp_path / "nonexistent.key"
        dummy_cert = tmp_path / "nonexistent.crt"

        payload = b'{"capsule_id": "test-override"}'
        envelope_bytes = create_envelope(
            payload, dummy, dummy_cert, backend=backend
        )
        assert isinstance(envelope_bytes, bytes)


# ---------------------------------------------------------------------------
# Backward-compatibility: existing profile: local YAML still works
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    def test_existing_local_profile_yaml_unchanged(self, ec_key_and_cert, tmp_path):
        """profile: local yaml still produces a valid SigningProfile (no regression)."""
        from novafabric.trust.novaseal.config import (
            _DEFAULT_TSA_URL,
            SigningProfile,
            _parse_profile,
        )

        key_path, cert_path, _ = ec_key_and_cert
        cfg = tmp_path / "novaseal.yaml"
        cfg.write_text(
            f"profile: local\n"
            f"key_path: {key_path}\n"
            f"cert_path: {cert_path}\n"
        )
        profile = _parse_profile(cfg)

        assert isinstance(profile, SigningProfile)
        assert profile.profile == "local"
        assert profile.key_path == key_path
        assert profile.cert_path == cert_path
        assert profile.tsa_url == _DEFAULT_TSA_URL
        assert profile.kms_key_id is None
        assert profile.vault_url is None
        assert profile.key_version_name is None
