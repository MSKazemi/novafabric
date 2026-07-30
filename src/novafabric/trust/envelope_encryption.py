# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Application-layer envelope encryption for blob payloads (ADR-0185, experimental).

Per-object envelope scheme:

- Each :func:`encrypt_blob` call generates a **fresh random 256-bit DEK** and
  encrypts the payload with AES-256-GCM (96-bit random nonce) via the existing
  ``cryptography`` dependency — no new dependency.
- The DEK is wrapped by a KEK held in the operator's KMS through the additive
  :class:`~novafabric.trust.novaseal.signing_backend.KeyWrappingBackend`
  capability; the wrapped DEK travels inside the :class:`EncryptedBlob`.
- ``content_sha256`` is computed **over the ciphertext** (normative, ADR-0185
  encrypt-before-WORM invariant): integrity verification — hashes, Merkle
  leaves, WORM conformance — never requires decryption or KMS access.
- :func:`shred` implements single-key-deletion crypto-shred semantics
  (ADR-0134 synergy): removing the wrapped DEK renders the ciphertext
  permanently unrecoverable while leaving it intact inside its WORM window.

This module is the crypto layer only and is **not the default**.  Opt-in
store wiring exists via
:class:`novafabric.object_capsule_store.encryption_wrapper.EncryptingAdapter`
(second slice).  The cloud-KMS wrap paths (AWS/Azure/GCP) are implemented in
:mod:`novafabric.trust.novaseal.signing_backend` and verified against
in-memory fakes of each SDK; only end-to-end verification against live
cloud credentials remains deferred.
"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Literal

from pydantic import BaseModel, Field

from novafabric.trust.novaseal.signing_backend import KeyWrappingBackend

__all__ = [
    "BlobAuthenticationError",
    "CiphertextIntegrityError",
    "DekUnwrapError",
    "EncryptedBlob",
    "EnvelopeEncryptionError",
    "ShreddedBlobError",
    "decrypt_blob",
    "encrypt_blob",
    "shred",
    "verify_ciphertext_hash",
]

_ALGO: Literal["AES-256-GCM"] = "AES-256-GCM"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class EnvelopeEncryptionError(Exception):
    """Base class for envelope-encryption failures."""


class ShreddedBlobError(EnvelopeEncryptionError):
    """The blob was crypto-shredded: its wrapped DEK is gone, decryption is impossible."""


class BlobAuthenticationError(EnvelopeEncryptionError):
    """AES-GCM authentication failed: ciphertext, nonce, or DEK was tampered with."""


class DekUnwrapError(EnvelopeEncryptionError):
    """The KMS backend could not unwrap the wrapped DEK (tampered or wrong KEK)."""


class CiphertextIntegrityError(EnvelopeEncryptionError):
    """The ciphertext does not match the recorded ``content_sha256``."""


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class EncryptedBlob(BaseModel):
    """An envelope-encrypted payload with its KMS-wrapped per-object DEK.

    Binary fields are stored base64-encoded so the model serializes losslessly
    to JSON (manifest/column-metadata transport).  ``content_sha256`` is the
    hex SHA-256 of the **ciphertext** — verifiable without any key material.
    """

    algo: Literal["AES-256-GCM"] = Field(
        default=_ALGO, description="AEAD algorithm tag for the payload encryption"
    )
    nonce_b64: str = Field(..., description="Base64 96-bit AES-GCM nonce")
    ciphertext_b64: str = Field(..., description="Base64 ciphertext including the GCM tag")
    wrapped_dek_b64: str | None = Field(
        ...,
        description=(
            "Base64 KMS-wrapped per-object DEK; None after crypto-shred (ADR-0134 semantics)"
        ),
    )
    kek_ref: str = Field(..., description="Non-secret identifier of the wrapping KEK/backend")
    content_sha256: str = Field(
        ..., description="Hex SHA-256 computed over the CIPHERTEXT (never the plaintext)"
    )
    shredded: bool = Field(
        default=False, description="True once the wrapped DEK has been crypto-shredded"
    )

    model_config = {"frozen": True}

    @property
    def nonce(self) -> bytes:
        """Decoded AES-GCM nonce bytes."""
        return base64.b64decode(self.nonce_b64)

    @property
    def ciphertext(self) -> bytes:
        """Decoded ciphertext bytes (including the GCM tag)."""
        return base64.b64decode(self.ciphertext_b64)

    @property
    def wrapped_dek(self) -> bytes | None:
        """Decoded wrapped-DEK bytes, or ``None`` when shredded."""
        if self.wrapped_dek_b64 is None:
            return None
        return base64.b64decode(self.wrapped_dek_b64)


# ---------------------------------------------------------------------------
# Capability guard
# ---------------------------------------------------------------------------


def _require_wrap_capable(backend: object) -> KeyWrappingBackend:
    """Return *backend* if it implements the KMS wrap capability, else raise.

    Raises:
        NotImplementedError: when *backend* does not provide the additive
            ``wrap_key``/``unwrap_key``/``kek_ref`` capability (ADR-0185).
    """
    if isinstance(backend, KeyWrappingBackend):
        return backend
    raise NotImplementedError(
        f"Backend {type(backend).__name__} does not implement the KMS key-wrapping "
        "capability (wrap_key/unwrap_key/kek_ref) required for envelope encryption. "
        "Use LocalSigningBackend with a kek_path, MockKmsBackend (tests), or one of "
        "the cloud backends in novafabric.trust.novaseal.signing_backend: "
        "AwsKmsWrappingBackend, AzureKvWrappingBackend, GcpKmsWrappingBackend "
        "(ADR-0185)."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def encrypt_blob(plaintext: bytes, *, backend: KeyWrappingBackend) -> EncryptedBlob:
    """Encrypt *plaintext* under a fresh per-object DEK wrapped by *backend*.

    A new random 256-bit DEK and 96-bit nonce are generated per call, so two
    encryptions of identical plaintext always produce distinct ciphertexts and
    distinct wrapped DEKs.

    Args:
        plaintext: Raw payload bytes to protect.
        backend:   A backend implementing the ``KeyWrappingBackend`` capability.

    Raises:
        NotImplementedError: if *backend* lacks the wrap capability.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    kms = _require_wrap_capable(backend)
    dek = os.urandom(32)
    nonce = os.urandom(12)
    ciphertext = AESGCM(dek).encrypt(nonce, plaintext, None)
    wrapped_dek = kms.wrap_key(dek)
    return EncryptedBlob(
        algo=_ALGO,
        nonce_b64=base64.b64encode(nonce).decode("ascii"),
        ciphertext_b64=base64.b64encode(ciphertext).decode("ascii"),
        wrapped_dek_b64=base64.b64encode(wrapped_dek).decode("ascii"),
        kek_ref=kms.kek_ref(),
        content_sha256=hashlib.sha256(ciphertext).hexdigest(),
        shredded=False,
    )


def decrypt_blob(blob: EncryptedBlob, *, backend: KeyWrappingBackend) -> bytes:
    """Unwrap the blob's DEK via *backend* and return the decrypted plaintext.

    Raises:
        ShreddedBlobError:        the blob was crypto-shredded (no wrapped DEK).
        CiphertextIntegrityError: ciphertext does not match ``content_sha256``.
        DekUnwrapError:           the backend failed to unwrap the DEK.
        BlobAuthenticationError:  AES-GCM authentication failed (tampering).
        NotImplementedError:      *backend* lacks the wrap capability.
    """
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    kms = _require_wrap_capable(backend)
    wrapped_dek = blob.wrapped_dek
    if blob.shredded or wrapped_dek is None:
        raise ShreddedBlobError(
            f"Blob (kek_ref={blob.kek_ref!r}) was crypto-shredded: its wrapped DEK was "
            "deleted (ADR-0134 single-key-deletion semantics) and the ciphertext is "
            "permanently unrecoverable."
        )
    ciphertext = blob.ciphertext
    if hashlib.sha256(ciphertext).hexdigest() != blob.content_sha256:
        raise CiphertextIntegrityError(
            "Ciphertext does not match the recorded content_sha256; refusing to decrypt."
        )
    try:
        dek = kms.unwrap_key(wrapped_dek)
    except EnvelopeEncryptionError:
        # Already a typed envelope error from the backend — propagate as-is.
        raise
    except Exception as exc:
        # Any unwrap failure is a DEK-unwrap failure — regardless of which backend
        # raised it. The local/mock backends raise cryptography.InvalidTag; the
        # AWS/Azure/GCP backends raise their own SDK exceptions (botocore
        # ClientError, Azure/GCP errors, transport failures). All of them mean
        # "the DEK could not be unwrapped", and none must leak a raw SDK exception
        # (or its message, which can carry backend internals) to the caller.
        raise DekUnwrapError(
            f"Backend {type(backend).__name__} failed to unwrap the DEK "
            f"(kek_ref={blob.kek_ref!r}): wrapped key tampered, wrong KEK, or the "
            "KMS backend rejected/could not complete the unwrap."
        ) from exc
    try:
        return AESGCM(dek).decrypt(blob.nonce, ciphertext, None)
    except InvalidTag as exc:
        raise BlobAuthenticationError(
            "AES-256-GCM authentication failed: ciphertext or nonce was tampered with, "
            "or the unwrapped DEK does not match."
        ) from exc


def shred(blob: EncryptedBlob) -> EncryptedBlob:
    """Return a crypto-shredded copy of *blob*: wrapped DEK removed, ciphertext intact.

    Single-key-deletion semantics per ADR-0134 synergy: deleting the one
    wrapped DEK erases the object while the ciphertext stays untouched inside
    its WORM retention window and still verifies via ``content_sha256``.
    Idempotent: shredding an already-shredded blob returns an equal copy.
    """
    return blob.model_copy(update={"wrapped_dek_b64": None, "shredded": True})


def verify_ciphertext_hash(blob: EncryptedBlob) -> bool:
    """Verify ``content_sha256`` over the ciphertext — no key material required."""
    return hashlib.sha256(blob.ciphertext).hexdigest() == blob.content_sha256
