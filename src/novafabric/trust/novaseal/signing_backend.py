"""Cloud KMS and local signing backend abstraction for NovaSeal v1.

Provides a ``SigningBackend`` Protocol and four implementations:

- ``LocalSigningBackend``  — ECDSA P-256 key + cert loaded from PEM files.
- ``AwsKmsSigningBackend`` — AWS KMS asymmetric key (ECDSA_SHA_256), requires
                            ``pip install novafabric[seal-aws]``.
- ``AzureKvSigningBackend``— Azure Key Vault ECDSA key, requires
                            ``pip install novafabric[seal-azure]``.
- ``GcpKmsSigningBackend`` — GCP Cloud KMS asymmetric sign, requires
                            ``pip install novafabric[seal-gcp]``.

All cloud backends store the X.509 certificate locally (the cloud KMS does not
issue one automatically); operators must export/obtain the cert for their KMS key
and point ``cert_path`` at it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable  # noqa: UP035

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class SigningBackend(Protocol):
    """Minimal interface for a NovaSeal signing backend."""

    def sign_digest(self, digest: bytes) -> bytes:
        """Sign a SHA-256 *digest*; return raw DER-encoded ECDSA P-256 signature."""
        ...

    def get_cert_der(self) -> bytes:
        """Return the DER-encoded X.509 certificate for key identity."""
        ...


# ---------------------------------------------------------------------------
# Local (PEM file) backend
# ---------------------------------------------------------------------------

class LocalSigningBackend:
    """Signs with a local ECDSA P-256 PEM private key and X.509 certificate.

    Args:
        key_path:  Path to the ECDSA P-256 PEM private key file.
        cert_path: Path to the PEM-encoded X.509 certificate.
    """

    def __init__(self, key_path: Path, cert_path: Path) -> None:
        self._key_path = key_path
        self._cert_path = cert_path

    def sign_digest(self, digest: bytes) -> bytes:
        """Sign *digest* with ECDSA P-256 / SHA-256; return DER signature.

        The *digest* parameter must be a 32-byte SHA-256 digest of the message
        to sign.  The underlying cryptography call uses ``Prehashed`` so the
        library does not hash the input a second time.
        """
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ec import ECDSA, EllipticCurvePrivateKey
        from cryptography.hazmat.primitives.asymmetric.utils import Prehashed
        from cryptography.hazmat.primitives.hashes import SHA256

        pem = self._key_path.read_bytes()
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, EllipticCurvePrivateKey):
            raise TypeError(
                f"LocalSigningBackend requires an EC private key, got {type(key).__name__}"
            )
        # Prehashed tells cryptography the input is already a digest.
        return key.sign(digest, ECDSA(Prehashed(SHA256())))

    def get_cert_der(self) -> bytes:
        """Return DER bytes of the X.509 certificate."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.x509 import load_pem_x509_certificate

        pem = self._cert_path.read_bytes()
        cert = load_pem_x509_certificate(pem)
        return cert.public_bytes(serialization.Encoding.DER)


# ---------------------------------------------------------------------------
# AWS KMS backend
# ---------------------------------------------------------------------------

class AwsKmsSigningBackend:
    """Signs with an AWS KMS asymmetric key (ECDSA_SHA_256).

    The KMS key must be a P-256 asymmetric signing key.  The X.509 certificate
    is read from *cert_path* — export it from the KMS console or via
    ``aws kms get-public-key`` and wrap it in a self-signed cert.

    Args:
        key_id:    AWS KMS key ARN or alias/ARN.
        cert_path: Path to the PEM-encoded X.509 certificate for this key.
        region:    AWS region name (default ``"us-east-1"``).

    Requires: ``pip install novafabric[seal-aws]``  (boto3>=1.38.0)
    """

    def __init__(self, key_id: str, cert_path: Path, region: str = "us-east-1") -> None:
        self._key_id = key_id
        self._cert_path = cert_path
        self._region = region
        self._client: Any = None  # lazily initialised

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import boto3  # noqa: F401
            except ImportError as exc:
                raise ImportError(
                    "AWS KMS backend requires boto3. "
                    "Install it with: pip install novafabric[seal-aws]"
                ) from exc
            import boto3
            self._client = boto3.client("kms", region_name=self._region)
        return self._client

    def sign_digest(self, digest: bytes) -> bytes:
        """Sign *digest* via AWS KMS; return DER-encoded ECDSA signature."""
        client = self._get_client()
        resp = client.sign(
            KeyId=self._key_id,
            Message=digest,
            MessageType="DIGEST",
            SigningAlgorithm="ECDSA_SHA_256",
        )
        return bytes(resp["Signature"])

    def get_cert_der(self) -> bytes:
        """Return DER bytes of the local X.509 certificate."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.x509 import load_pem_x509_certificate

        pem = self._cert_path.read_bytes()
        cert = load_pem_x509_certificate(pem)
        return cert.public_bytes(serialization.Encoding.DER)


# ---------------------------------------------------------------------------
# Azure Key Vault backend
# ---------------------------------------------------------------------------

class AzureKvSigningBackend:
    """Signs with an Azure Key Vault EC P-256 key.

    Args:
        vault_url:  Azure Key Vault URL, e.g. ``https://myvault.vault.azure.net/``.
        key_name:   Name of the key in Key Vault.
        cert_path:  Path to the PEM-encoded X.509 certificate for this key.

    Requires: ``pip install novafabric[seal-azure]``  (azure-keyvault-keys>=4.9.0)
    """

    def __init__(self, vault_url: str, key_name: str, cert_path: Path) -> None:
        self._vault_url = vault_url
        self._key_name = key_name
        self._cert_path = cert_path

    def sign_digest(self, digest: bytes) -> bytes:
        """Sign *digest* via Azure Key Vault; return DER-encoded ECDSA signature."""
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.keys import KeyClient
            from azure.keyvault.keys.crypto import CryptographyClient, SignatureAlgorithm
        except ImportError as exc:
            raise ImportError(
                "Azure Key Vault backend requires azure-keyvault-keys and azure-identity. "
                "Install with: pip install novafabric[seal-azure]"
            ) from exc

        credential = DefaultAzureCredential()
        key_client = KeyClient(vault_url=self._vault_url, credential=credential)
        key = key_client.get_key(self._key_name)
        crypto_client = CryptographyClient(key, credential=credential)
        result = crypto_client.sign(SignatureAlgorithm.es256, digest)
        return bytes(result.signature)

    def get_cert_der(self) -> bytes:
        """Return DER bytes of the local X.509 certificate."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.x509 import load_pem_x509_certificate

        pem = self._cert_path.read_bytes()
        cert = load_pem_x509_certificate(pem)
        return cert.public_bytes(serialization.Encoding.DER)


# ---------------------------------------------------------------------------
# GCP Cloud KMS backend
# ---------------------------------------------------------------------------

class GcpKmsSigningBackend:
    """Signs with a GCP Cloud KMS asymmetric EC P-256 key.

    Args:
        key_version_name: Full resource name, e.g.
            ``projects/P/locations/L/keyRings/R/cryptoKeys/K/cryptoKeyVersions/1``
        cert_path: Path to the PEM-encoded X.509 certificate for this key.

    Requires: ``pip install novafabric[seal-gcp]``  (google-cloud-kms>=3.3.0)
    """

    def __init__(self, key_version_name: str, cert_path: Path) -> None:
        self._key_version_name = key_version_name
        self._cert_path = cert_path

    def sign_digest(self, digest: bytes) -> bytes:
        """Sign *digest* via GCP KMS; return DER-encoded ECDSA signature."""
        try:
            from google.cloud import kms as gcp_kms
        except ImportError as exc:
            raise ImportError(
                "GCP KMS backend requires google-cloud-kms. "
                "Install with: pip install novafabric[seal-gcp]"
            ) from exc

        client = gcp_kms.KeyManagementServiceClient()
        response = client.asymmetric_sign(
            name=self._key_version_name,
            digest=gcp_kms.Digest(sha256=digest),
        )
        return bytes(response.signature)

    def get_cert_der(self) -> bytes:
        """Return DER bytes of the local X.509 certificate."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.x509 import load_pem_x509_certificate

        pem = self._cert_path.read_bytes()
        cert = load_pem_x509_certificate(pem)
        return cert.public_bytes(serialization.Encoding.DER)
