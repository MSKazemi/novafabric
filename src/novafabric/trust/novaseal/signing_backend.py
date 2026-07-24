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

ADR-0185 (experimental) adds an *additive, optional* KMS capability protocol,
``KeyWrappingBackend`` (``wrap_key``/``unwrap_key``/``kek_ref``), used by
``novafabric.trust.envelope_encryption`` for per-object DEK wrapping.  It is
implemented by ``LocalSigningBackend`` (when given a ``kek_path``), by the
test-only ``MockKmsBackend``, and by ``AwsKmsWrappingBackend`` (real AWS KMS
``Encrypt``/``Decrypt``, moto-tested; the Azure/GCP wrap paths remain planned).
The ``SigningBackend`` Protocol itself is unchanged.
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


@runtime_checkable
class KeyWrappingBackend(Protocol):
    """Additive, optional KMS capability: wrap/unwrap a data-encryption key.

    Part of ADR-0185 (application-layer envelope encryption at rest,
    **experimental**).  This is a *separate* capability protocol — the
    ``SigningBackend`` Protocol above is deliberately untouched per ADR-0185,
    and backends that do not implement wrapping simply cannot be selected for
    envelope encryption (``novafabric.trust.envelope_encryption`` raises
    ``NotImplementedError`` with a clear message for such backends).

    Implemented today by :class:`LocalSigningBackend` (KEK from a local key
    file, for dev/test parity) and :class:`MockKmsBackend` (in-memory KEK, for
    tests).  The AWS/Azure/GCP wrap paths (KMS Encrypt/Decrypt, Key Vault
    wrapKey/unwrapKey, Cloud KMS encrypt/decrypt) are **planned** and
    infra-gated on live cloud credentials (ADR-0185 Bucket C).
    """

    def wrap_key(self, plaintext_key: bytes) -> bytes:
        """Wrap (encrypt) *plaintext_key* under the backend's KEK; return opaque bytes."""
        ...

    def unwrap_key(self, wrapped_key: bytes) -> bytes:
        """Unwrap *wrapped_key* (produced by :meth:`wrap_key`); return the plaintext key."""
        ...

    def kek_ref(self) -> str:
        """Return a stable, non-secret identifier for the KEK (for audit/manifest use)."""
        ...


def _aes_gcm_wrap(kek: bytes, plaintext_key: bytes) -> bytes:
    """Wrap *plaintext_key* with AES-256-GCM under *kek*: ``nonce(12) || ct+tag``."""
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    return nonce + AESGCM(kek).encrypt(nonce, plaintext_key, None)


def _aes_gcm_unwrap(kek: bytes, wrapped_key: bytes) -> bytes:
    """Reverse :func:`_aes_gcm_wrap`; raises ``cryptography.exceptions.InvalidTag`` on tamper."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(wrapped_key) < 13:
        raise ValueError("wrapped key too short: expected nonce(12) || ciphertext+tag")
    return AESGCM(kek).decrypt(wrapped_key[:12], wrapped_key[12:], None)


def _kek_fingerprint(kek: bytes) -> str:
    """Return a short non-secret fingerprint of *kek* for ``kek_ref`` identifiers."""
    import hashlib

    return hashlib.sha256(kek).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Local (PEM file) backend
# ---------------------------------------------------------------------------

class LocalSigningBackend:
    """Signs with a local ECDSA P-256 PEM private key and X.509 certificate.

    Optionally implements the :class:`KeyWrappingBackend` capability (ADR-0185,
    experimental) when *kek_path* is provided: the KEK is a 256-bit AES key
    read from a local file (32 raw bytes, or 64 hex characters), used to wrap
    per-object DEKs with AES-256-GCM.  For dev/test parity only — production
    deployments should use a cloud-KMS-held KEK (planned, infra-gated).

    Args:
        key_path:  Path to the ECDSA P-256 PEM private key file.
        cert_path: Path to the PEM-encoded X.509 certificate.
        kek_path:  Optional path to a local 256-bit KEK file (raw 32 bytes or
                   64 hex chars).  Without it, ``wrap_key``/``unwrap_key``
                   raise ``NotImplementedError``.
    """

    def __init__(self, key_path: Path, cert_path: Path, kek_path: Path | None = None) -> None:
        self._key_path = key_path
        self._cert_path = cert_path
        self._kek_path = kek_path

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

    # -- KeyWrappingBackend capability (ADR-0185, experimental) --------------

    def _load_kek(self) -> bytes:
        if self._kek_path is None:
            raise NotImplementedError(
                "LocalSigningBackend was constructed without a kek_path, so it has no "
                "KMS key-wrapping capability (wrap_key/unwrap_key). Pass kek_path= a "
                "local 256-bit KEK file to enable envelope encryption (ADR-0185)."
            )
        raw = self._kek_path.read_bytes()
        if len(raw) == 32:
            return raw
        text = raw.decode("ascii", errors="strict").strip() if len(raw) <= 66 else ""
        if len(text) == 64:
            return bytes.fromhex(text)
        raise ValueError(
            f"KEK file {self._kek_path} must contain exactly 32 raw bytes or 64 hex "
            f"characters (got {len(raw)} bytes)"
        )

    def wrap_key(self, plaintext_key: bytes) -> bytes:
        """Wrap *plaintext_key* with AES-256-GCM under the local file KEK."""
        return _aes_gcm_wrap(self._load_kek(), plaintext_key)

    def unwrap_key(self, wrapped_key: bytes) -> bytes:
        """Unwrap a key previously wrapped by :meth:`wrap_key`."""
        return _aes_gcm_unwrap(self._load_kek(), wrapped_key)

    def kek_ref(self) -> str:
        """Return a non-secret identifier for the local KEK (fingerprint, not path)."""
        return f"local-kek:{_kek_fingerprint(self._load_kek())}"


# ---------------------------------------------------------------------------
# Mock KMS backend (tests / CI — ADR-0185 Bucket B)
# ---------------------------------------------------------------------------

class MockKmsBackend:
    """In-memory KMS fake implementing the :class:`KeyWrappingBackend` capability.

    For tests and CI only (ADR-0185 "Bucket B" verification split): a real
    AES-256-GCM wrap/unwrap over an in-memory KEK, with no cloud SDK and no
    filesystem state.  Never use in production — the KEK lives in process
    memory and vanishes with the instance.

    Args:
        kek: Optional 32-byte KEK; a random one is generated when omitted.
    """

    def __init__(self, kek: bytes | None = None) -> None:
        import os

        if kek is None:
            kek = os.urandom(32)
        if len(kek) != 32:
            raise ValueError(f"MockKmsBackend KEK must be 32 bytes, got {len(kek)}")
        self._kek = kek

    def wrap_key(self, plaintext_key: bytes) -> bytes:
        """Wrap *plaintext_key* with AES-256-GCM under the in-memory KEK."""
        return _aes_gcm_wrap(self._kek, plaintext_key)

    def unwrap_key(self, wrapped_key: bytes) -> bytes:
        """Unwrap a key previously wrapped by :meth:`wrap_key`."""
        return _aes_gcm_unwrap(self._kek, wrapped_key)

    def kek_ref(self) -> str:
        """Return a non-secret identifier for the in-memory KEK."""
        return f"mock-kms:{_kek_fingerprint(self._kek)}"


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


# ---------------------------------------------------------------------------
# AWS KMS key-wrapping backend (envelope encryption, ADR-0185)
# ---------------------------------------------------------------------------

class AwsKmsWrappingBackend:
    """:class:`KeyWrappingBackend` backed by an AWS KMS symmetric key.

    Wraps a data-encryption key by calling KMS ``Encrypt`` (and ``Decrypt`` to
    unwrap), so the plaintext KEK never leaves KMS — the process only ever holds
    the DEK and the opaque KMS ciphertext blob. This implements the AWS branch of
    ADR-0185 that :class:`LocalSigningBackend` / :class:`MockKmsBackend` stand in
    for locally; it is exercised in tests against an in-process AWS mock (moto).

    Args:
        key_id: AWS KMS key id, alias (``alias/...``), or ARN of a **symmetric**
            encryption key (``KeyUsage=ENCRYPT_DECRYPT``).
        region: AWS region name (default ``"us-east-1"``).

    Requires: ``pip install novafabric[seal-aws]`` (boto3>=1.38.0).
    """

    def __init__(self, key_id: str, region: str = "us-east-1") -> None:
        self._key_id = key_id
        self._region = region
        self._client: Any = None  # lazily initialised

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:
                raise ImportError(
                    "AWS KMS wrapping backend requires boto3. "
                    "Install it with: pip install novafabric[seal-aws]"
                ) from exc
            self._client = boto3.client("kms", region_name=self._region)
        return self._client

    def wrap_key(self, plaintext_key: bytes) -> bytes:
        """Wrap *plaintext_key* via KMS ``Encrypt``; return the opaque ciphertext blob."""
        resp = self._get_client().encrypt(KeyId=self._key_id, Plaintext=plaintext_key)
        return bytes(resp["CiphertextBlob"])

    def unwrap_key(self, wrapped_key: bytes) -> bytes:
        """Unwrap *wrapped_key* via KMS ``Decrypt``; return the plaintext key."""
        resp = self._get_client().decrypt(KeyId=self._key_id, CiphertextBlob=wrapped_key)
        return bytes(resp["Plaintext"])

    def kek_ref(self) -> str:
        """Return a stable, non-secret identifier for the KMS KEK (region + key id)."""
        return f"aws-kms:{self._region}:{self._key_id}"
