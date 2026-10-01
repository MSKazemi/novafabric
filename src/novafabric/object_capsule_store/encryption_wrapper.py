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
"""Opt-in envelope-encryption wrapper for WORM adapters (ADR-0185, experimental).

``EncryptingAdapter`` decorates any :class:`~novafabric.object_capsule_store.worm.base.WormAdapter`
with ADR-0185 application-layer envelope encryption:

- **put:** the plaintext is encrypted under a fresh per-object DEK
  (:func:`novafabric.trust.envelope_encryption.encrypt_blob`), the resulting
  :class:`~novafabric.trust.envelope_encryption.EncryptedBlob` is serialized to
  JSON, and *those ciphertext-envelope bytes* are what the inner WORM adapter
  stores — the encrypt-before-WORM-write invariant.  The SHA-256 handed to the
  backend (checksum header / CAS gate) is recomputed **over the stored
  (encrypted) bytes**, so integrity verification never needs the KEK.
- **get:** stored bytes are loaded, the envelope format is detected via its
  schema marker fields, and the payload is decrypted back to plaintext.
  New envelopes are **v2**, bound to their object key as AES-GCM associated
  data (ADR-0290): an envelope copied to another key fails authentication.
  Legacy **v1** envelopes still decrypt and are flagged (warning log +
  :attr:`EncryptingAdapter.legacy_envelope_reads`).
  A crypto-shredded envelope raises the named
  :class:`~novafabric.trust.envelope_encryption.ShreddedBlobError`.
- **non-envelope bytes fail closed (ADR-0290):** outside the chain-log
  namespace, a stored object that is not an envelope raises
  :class:`PlaintextObjectRefusedError` — a reader cannot tell "written before
  encryption was enabled" from "plaintext substituted by someone with write
  access to the inner store".  Mixed stores holding pre-encryption objects
  opt in explicitly with ``allow_plaintext_reads=True``
  (``NOVA_OBJECT_STORE_ALLOW_PLAINTEXT_READS=1``); every such read is logged.
- **chain-log objects** (``put_log_object*``, the ``_capsule_log/``
  namespace) are integrity metadata, not capsule payloads — they pass through
  unencrypted, exactly as ADR-0031 excludes them from WORM.

This wrapper is **not the default** (ADR-0185): it is only engaged via
explicit opt-in configuration (see ``backend_router.make_adapter``), because
envelope encryption trades confidentiality for a key-availability risk —
lose the KEK, lose the evidence.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING

from novafabric.object_capsule_store.cas import compute_sha256
from novafabric.object_capsule_store.exceptions import CASMismatchError
from novafabric.object_capsule_store.worm.base import WormAdapter, WormPutResult
from novafabric.trust.envelope_encryption import (
    EncryptedBlob,
    EnvelopeEncryptionError,
    decrypt_blob,
    encrypt_blob,
)
from novafabric.trust.novaseal.signing_backend import KeyWrappingBackend

if TYPE_CHECKING:
    from novafabric.trust.tenant_keys import TenantKeyRegistry

log = logging.getLogger(__name__)

__all__ = ["CHAIN_LOG_PREFIX", "EncryptingAdapter", "PlaintextObjectRefusedError"]

#: Key namespace of chain-log / checkpoint objects (``manifest_chain``,
#: ``checkpoint``). Written via ``put_log_object*`` and never encrypted.
CHAIN_LOG_PREFIX = "_capsule_log/"


class PlaintextObjectRefusedError(EnvelopeEncryptionError):
    """A non-envelope object was read from an encrypted store (ADR-0290, fail closed).

    Raised instead of returning bytes that may have been substituted by
    anyone with write access to the inner store.  Pre-encryption objects in
    a mixed store are readable only with the explicit legacy opt-in
    (``allow_plaintext_reads=True`` / ``NOVA_OBJECT_STORE_ALLOW_PLAINTEXT_READS=1``).
    """

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(
            f"object {key!r} is not an encryption envelope but the store is "
            "configured for envelope encryption; refusing to return unauthenticated "
            "plaintext (ADR-0290). If this store holds objects written before "
            "encryption was enabled, set NOVA_OBJECT_STORE_ALLOW_PLAINTEXT_READS=1 "
            "for the migration window."
        )

# Marker fields that identify a stored object as a serialized EncryptedBlob
# envelope (schema-field detection; raw pre-encryption objects lack these).
_ENVELOPE_MARKER_FIELDS = frozenset(
    {"algo", "nonce_b64", "ciphertext_b64", "content_sha256", "kek_ref"}
)
_ENVELOPE_ALGO = "AES-256-GCM"


class EncryptingAdapter(WormAdapter):
    """Envelope-encrypting decorator around a concrete ``WormAdapter``.

    Args:
        inner:   The wrapped WORM adapter that performs the actual storage IO.
        backend: A KMS-capable backend implementing the additive
                 :class:`~novafabric.trust.novaseal.signing_backend.KeyWrappingBackend`
                 capability (``wrap_key``/``unwrap_key``/``kek_ref``), e.g.
                 ``LocalSigningBackend(kek_path=...)`` or ``MockKmsBackend``.
        tenant_keys: Optional ADR-0243 per-tenant KEK registry.
        allow_plaintext_reads: Legacy-migration opt-in (ADR-0290). ``False``
                 (default) refuses non-envelope objects outside the chain-log
                 namespace with :class:`PlaintextObjectRefusedError`.
    """

    def __init__(
        self,
        inner: WormAdapter,
        backend: KeyWrappingBackend,
        tenant_keys: "TenantKeyRegistry | None" = None,
        *,
        allow_plaintext_reads: bool = False,
    ) -> None:
        self._inner = inner
        self._backend = backend
        self._allow_plaintext_reads = allow_plaintext_reads
        #: Count of legacy unbound (v1) envelopes decrypted by this adapter.
        self.legacy_envelope_reads = 0
        #: Count of non-envelope objects returned under the legacy opt-in.
        self.plaintext_reads = 0
        # ADR-0243 slice 1: optional per-tenant KEK resolution. None keeps the
        # flat single-backend behavior byte-for-byte.
        self._tenant_keys = tenant_keys

    # -----------------------------------------------------------------------
    # Write path — encrypt before the WORM write (ADR-0185 normative ordering)
    # -----------------------------------------------------------------------

    def _encrypt(self, key: str, data: bytes, sha256_hex: str) -> tuple[bytes, str]:
        """Encrypt *data* and return ``(stored_bytes, stored_sha256)``.

        The caller-asserted *sha256_hex* (computed over the plaintext) is
        validated first so client-side CAS (FR-14) still fails fast before any
        network call; the SHA-256 forwarded to the inner adapter is then
        recomputed over the serialized ciphertext envelope — hashes address
        the CIPHERTEXT, never the plaintext (ADR-0185).
        """
        computed = compute_sha256(data)
        if computed != sha256_hex:
            raise CASMismatchError(key=key, expected=sha256_hex, observed=computed)
        backend = self._backend
        tenant_key_id: str | None = None
        if self._tenant_keys is not None:
            from novafabric.trust.tenant_keys import tenant_from_object_key

            backend, tenant_key_id = self._tenant_keys.backend_for_write(
                tenant_from_object_key(key)
            )
        blob = encrypt_blob(data, backend=backend, tenant_key_id=tenant_key_id, object_id=key)
        stored = blob.model_dump_json().encode("utf-8")
        return stored, compute_sha256(stored)

    def put_object(
        self,
        key: str,
        data: bytes,
        sha256_hex: str,
        retention_days: int,
        content_type: str = "application/octet-stream",
    ) -> WormPutResult:
        """Encrypt *data*, then WORM-write the ciphertext envelope to *key*."""
        stored, stored_sha = self._encrypt(key, data, sha256_hex)
        return self._inner.put_object(
            key, stored, stored_sha, retention_days, content_type="application/json"
        )

    def apply_identical(
        self,
        key_a: str,
        data_a: bytes,
        sha256_a: str,
        key_b: str,
        data_b: bytes,
        sha256_b: str,
        retention_days: int,
    ) -> tuple[WormPutResult, WormPutResult]:
        """Encrypt both payloads, then apply identical WORM policy (FR-07)."""
        stored_a, stored_sha_a = self._encrypt(key_a, data_a, sha256_a)
        stored_b, stored_sha_b = self._encrypt(key_b, data_b, sha256_b)
        return self._inner.apply_identical(
            key_a, stored_a, stored_sha_a, key_b, stored_b, stored_sha_b, retention_days
        )

    # -----------------------------------------------------------------------
    # Read path — envelope detection + transparent decryption
    # -----------------------------------------------------------------------

    @staticmethod
    def _parse_envelope(raw: bytes) -> EncryptedBlob | None:
        """Return the ``EncryptedBlob`` if *raw* is a stored envelope, else ``None``.

        Detection is by schema marker: a JSON object carrying all envelope
        fields with the expected AEAD algorithm tag.  Raw objects stored
        before encryption was enabled do not match and pass through.
        """
        if not raw.lstrip()[:1] == b"{":
            return None
        try:
            parsed = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        if not _ENVELOPE_MARKER_FIELDS <= parsed.keys():
            return None
        if parsed.get("algo") != _ENVELOPE_ALGO:
            return None
        return EncryptedBlob.model_validate(parsed)

    def get_object(self, key: str) -> bytes:
        """Fetch *key* and decrypt its envelope.

        Non-envelope bytes are returned only for chain-log keys
        (:data:`CHAIN_LOG_PREFIX`) or under the ``allow_plaintext_reads``
        legacy opt-in; otherwise the read fails closed.

        Raises:
            PlaintextObjectRefusedError: non-envelope object, no legacy opt-in.
            BlobAuthenticationError:  also raised when a v2 envelope was moved
                                      from another key (ADR-0290 binding).
            ShreddedBlobError:        the envelope was crypto-shredded (ADR-0134).
            DekUnwrapError:           the backend KEK cannot unwrap the DEK.
            CiphertextIntegrityError: envelope ciphertext fails its recorded hash.
            BlobAuthenticationError:  AES-GCM authentication failed (tampering).
        """
        raw = self._inner.get_object(key)
        blob = self._parse_envelope(raw)
        if blob is None:
            if key.startswith(CHAIN_LOG_PREFIX):
                return raw
            if not self._allow_plaintext_reads:
                raise PlaintextObjectRefusedError(key)
            self.plaintext_reads += 1
            log.warning(
                "returning non-envelope object %r from an encrypted store under the "
                "legacy plaintext-read opt-in (ADR-0290); re-ingest it to encrypt it",
                key,
            )
            return raw
        backend = (
            self._tenant_keys.backend_for_read(blob)
            if self._tenant_keys is not None
            else self._backend
        )
        plaintext = decrypt_blob(blob, backend=backend, object_id=key)
        if not blob.is_bound:
            self.legacy_envelope_reads += 1
            log.warning(
                "decrypted legacy unbound (v1) envelope %r: it is not bound to its "
                "object key and could have been copied from another object (ADR-0290)",
                key,
            )
        return plaintext

    # -----------------------------------------------------------------------
    # Chain-log + namespace operations — pass-through (never encrypted)
    # -----------------------------------------------------------------------

    def put_log_object(self, key: str, data: bytes) -> str:
        """Chain-log objects are integrity metadata — stored unencrypted."""
        return self._inner.put_log_object(key, data)

    def put_log_object_if_absent(self, key: str, data: bytes) -> str:
        """Conditional-PUT chain-log write — stored unencrypted (FR-02)."""
        return self._inner.put_log_object_if_absent(key, data)

    def list_objects(self, prefix: str) -> list[str]:
        return self._inner.list_objects(prefix)

    def iter_objects(self, prefix: str) -> Iterator[str]:
        yield from self._inner.iter_objects(prefix)

    def delete_object(self, key: str) -> None:
        """Delegate deletion — WORM-locked objects still raise (FR-07, FR-09)."""
        self._inner.delete_object(key)

    def object_exists(self, key: str) -> bool:
        return self._inner.object_exists(key)
