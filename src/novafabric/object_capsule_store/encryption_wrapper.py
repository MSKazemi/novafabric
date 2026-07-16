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
  Objects written *before* encryption was enabled (raw bytes, no envelope
  marker) pass through unchanged, so mixed-store reads keep working.
  A crypto-shredded envelope raises the named
  :class:`~novafabric.trust.envelope_encryption.ShreddedBlobError`.
- **chain-log objects** (``put_log_object*``) are integrity metadata, not
  capsule payloads — they pass through unencrypted, exactly as ADR-0031
  excludes them from WORM.

This wrapper is **not the default** (ADR-0185): it is only engaged via
explicit opt-in configuration (see ``backend_router.make_adapter``), because
envelope encryption trades confidentiality for a key-availability risk —
lose the KEK, lose the evidence.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

from novafabric.object_capsule_store.cas import compute_sha256
from novafabric.object_capsule_store.exceptions import CASMismatchError
from novafabric.object_capsule_store.worm.base import WormAdapter, WormPutResult
from novafabric.trust.envelope_encryption import (
    EncryptedBlob,
    decrypt_blob,
    encrypt_blob,
)
from novafabric.trust.novaseal.signing_backend import KeyWrappingBackend

log = logging.getLogger(__name__)

__all__ = ["EncryptingAdapter"]

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
    """

    def __init__(self, inner: WormAdapter, backend: KeyWrappingBackend) -> None:
        self._inner = inner
        self._backend = backend

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
        blob = encrypt_blob(data, backend=self._backend)
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
        """Fetch *key*; decrypt if it is an envelope, pass raw bytes through otherwise.

        Raises:
            ShreddedBlobError:        the envelope was crypto-shredded (ADR-0134).
            DekUnwrapError:           the backend KEK cannot unwrap the DEK.
            CiphertextIntegrityError: envelope ciphertext fails its recorded hash.
            BlobAuthenticationError:  AES-GCM authentication failed (tampering).
        """
        raw = self._inner.get_object(key)
        blob = self._parse_envelope(raw)
        if blob is None:
            return raw
        return decrypt_blob(blob, backend=self._backend)

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
