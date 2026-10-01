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
  (ADR-0134 synergy) **on the model**: it returns a copy without the wrapped
  DEK.  It cannot reach an envelope already stored inside its WORM retention
  window — the wrapped DEK is part of those immutable bytes — so erasing a
  *stored* object requires destroying the KEK that wrapped it (ADR-0243
  tenant-KEK revocation), not calling :func:`shred`.

Envelope versions (ADR-0290):

- **v2** (written whenever an ``object_id`` is supplied — always, from the
  object-capsule store) authenticates the object identity as AES-GCM
  *associated data*: an envelope copied to a different object key fails
  authentication.  The AAD binds the object key only — never ``kek_ref`` or
  ``tenant_key_id`` — so a future KEK re-wrap or KEK-hierarchy rotation
  (``kek-rotation-v0`` Track B) stays a metadata-only change.
- **v1** (no ``envelope_version`` field; ``aad=None``) is every envelope
  written before ADR-0290.  It still decrypts, but it is *unbound*:
  :attr:`EncryptedBlob.is_bound` is ``False`` and callers flag it.

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
    "ENVELOPE_VERSION_BOUND",
    "ENVELOPE_VERSION_LEGACY",
    "BlobAuthenticationError",
    "CiphertextIntegrityError",
    "DekUnwrapError",
    "EncryptedBlob",
    "EnvelopeBindingError",
    "EnvelopeEncryptionError",
    "ShreddedBlobError",
    "decrypt_blob",
    "encrypt_blob",
    "envelope_aad",
    "shred",
    "verify_ciphertext_hash",
]

_ALGO: Literal["AES-256-GCM"] = "AES-256-GCM"

#: Envelope written before ADR-0290: no associated data, not bound to its object.
ENVELOPE_VERSION_LEGACY: Literal[1] = 1
#: ADR-0290 envelope: the object identity is authenticated as AES-GCM AAD.
ENVELOPE_VERSION_BOUND: Literal[2] = 2

# Domain separator for the v2 AAD. Versioned so a future binding scheme can
# never be confused with this one.
_AAD_DOMAIN_V2 = b"novafabric/envelope-aad/v2\x00"


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


class EnvelopeBindingError(EnvelopeEncryptionError):
    """A bound (v2) envelope was decrypted without the object identity it is bound to."""


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
    tenant_key_id: str | None = Field(
        default=None,
        description=(
            "Tenant whose KEK wrapped this DEK (ADR-0243, additive). None means "
            "the flat ADR-0185 default KEK — every pre-0243 envelope stays valid."
        ),
    )

    envelope_version: Literal[1, 2] = Field(
        default=ENVELOPE_VERSION_LEGACY,
        description=(
            "Envelope format version (ADR-0290). 1 = legacy, no AAD (every "
            "pre-0290 envelope, which lacks this field); 2 = the object "
            "identity is authenticated as AES-GCM associated data."
        ),
    )

    model_config = {"frozen": True}

    @property
    def is_bound(self) -> bool:
        """True when the envelope is bound to its object identity (v2, ADR-0290)."""
        return self.envelope_version >= ENVELOPE_VERSION_BOUND

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
# Associated data (ADR-0290)
# ---------------------------------------------------------------------------


def envelope_aad(object_id: str) -> bytes:
    """The AES-GCM associated data binding a v2 envelope to *object_id*.

    ``domain separator || UTF-8(object_id)``.  Deliberately excludes
    ``kek_ref`` and ``tenant_key_id`` so re-wrapping a DEK under another KEK
    never requires re-encrypting the payload.

    Raises:
        EnvelopeBindingError: when *object_id* is empty.
    """
    if not object_id:
        raise EnvelopeBindingError("object_id must be a non-empty string (ADR-0290)")
    return _AAD_DOMAIN_V2 + object_id.encode("utf-8")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def encrypt_blob(
    plaintext: bytes,
    *,
    backend: KeyWrappingBackend,
    tenant_key_id: str | None = None,
    object_id: str | None = None,
) -> EncryptedBlob:
    """Encrypt *plaintext* under a fresh per-object DEK wrapped by *backend*.

    A new random 256-bit DEK and 96-bit nonce are generated per call, so two
    encryptions of identical plaintext always produce distinct ciphertexts and
    distinct wrapped DEKs.

    Args:
        plaintext: Raw payload bytes to protect.
        backend:   A backend implementing the ``KeyWrappingBackend`` capability.
        tenant_key_id: Tenant whose KEK wraps the DEK (ADR-0243), recorded only.
        object_id: Identity of the object this envelope will be stored as
                   (the object-store key).  When given, a **v2 bound**
                   envelope is produced (ADR-0290); when omitted, a legacy
                   unbound v1 envelope — kept only for API compatibility.

    Raises:
        NotImplementedError: if *backend* lacks the wrap capability.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    kms = _require_wrap_capable(backend)
    dek = os.urandom(32)
    nonce = os.urandom(12)
    aad = envelope_aad(object_id) if object_id is not None else None
    ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)
    wrapped_dek = kms.wrap_key(dek)
    return EncryptedBlob(
        algo=_ALGO,
        nonce_b64=base64.b64encode(nonce).decode("ascii"),
        ciphertext_b64=base64.b64encode(ciphertext).decode("ascii"),
        wrapped_dek_b64=base64.b64encode(wrapped_dek).decode("ascii"),
        kek_ref=kms.kek_ref(),
        content_sha256=hashlib.sha256(ciphertext).hexdigest(),
        shredded=False,
        tenant_key_id=tenant_key_id,
        envelope_version=(
            ENVELOPE_VERSION_BOUND if aad is not None else ENVELOPE_VERSION_LEGACY
        ),
    )


def decrypt_blob(
    blob: EncryptedBlob,
    *,
    backend: KeyWrappingBackend,
    object_id: str | None = None,
) -> bytes:
    """Unwrap the blob's DEK via *backend* and return the decrypted plaintext.

    A v2 (bound) envelope authenticates *object_id* as associated data: the
    caller must pass the identity it read the envelope from, and an envelope
    moved to another object fails with :class:`BlobAuthenticationError`.  A
    v1 (legacy) envelope ignores *object_id*.  Rewriting
    ``envelope_version`` from 2 to 1 does not help an attacker: the
    ciphertext was sealed with the AAD and fails authentication without it.

    Raises:
        EnvelopeBindingError:     a v2 envelope was decrypted without ``object_id``.
        ShreddedBlobError:        the blob was crypto-shredded (no wrapped DEK).
        CiphertextIntegrityError: ciphertext does not match ``content_sha256``.
        DekUnwrapError:           the backend failed to unwrap the DEK.
        BlobAuthenticationError:  AES-GCM authentication failed (tampering).
        NotImplementedError:      *backend* lacks the wrap capability.
    """
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    kms = _require_wrap_capable(backend)
    aad: bytes | None = None
    if blob.is_bound:
        if object_id is None:
            raise EnvelopeBindingError(
                "Envelope is bound to its object identity (v2, ADR-0290); "
                "decrypt_blob() needs the object_id it was read from."
            )
        aad = envelope_aad(object_id)
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
        return AESGCM(dek).decrypt(blob.nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise BlobAuthenticationError(
            "AES-256-GCM authentication failed: ciphertext or nonce was tampered with, "
            "the unwrapped DEK does not match"
            + (
                ", or the envelope was moved from another object (ADR-0290 binding)."
                if aad is not None
                else "."
            )
        ) from exc


def shred(blob: EncryptedBlob) -> EncryptedBlob:
    """Return a crypto-shredded copy of *blob*: wrapped DEK removed, ciphertext intact.

    Single-key-deletion semantics per ADR-0134 synergy: deleting the one
    wrapped DEK erases the object while the ciphertext stays untouched and
    still verifies via ``content_sha256``.
    Idempotent: shredding an already-shredded blob returns an equal copy.

    **Limitation (ADR-0290 §WORM):** this is a model operation.  It does not
    and cannot alter an envelope already written inside its WORM retention
    window — the stored bytes, wrapped DEK included, are immutable.  Erasing
    a stored object therefore means destroying the KEK that wrapped its DEK
    (ADR-0243 tenant-KEK revocation), which erases every object under that
    KEK.  Per-object erasure of stored capsules is **not** provided.
    """
    return blob.model_copy(update={"wrapped_dek_b64": None, "shredded": True})


def verify_ciphertext_hash(blob: EncryptedBlob) -> bool:
    """Verify ``content_sha256`` over the ciphertext — no key material required."""
    return hashlib.sha256(blob.ciphertext).hexdigest() == blob.content_sha256
