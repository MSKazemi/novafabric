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
"""Tests for ADR-0185 envelope encryption crypto layer (experimental).

Covers:
- round-trip encrypt/decrypt via MockKmsBackend
- unique per-object DEK (two encryptions of the same plaintext differ)
- tamper with ciphertext -> named GCM auth failure
- hash-over-ciphertext verification without any key material
- crypto-shred: shredded blob cannot decrypt, survives JSON serialization
- LocalSigningBackend wrap/unwrap round-trip from a local KEK file
- NotImplementedError with a clear message on a backend without KMS capability
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from novafabric.trust.envelope_encryption import (
    BlobAuthenticationError,
    CiphertextIntegrityError,
    DekUnwrapError,
    EncryptedBlob,
    ShreddedBlobError,
    decrypt_blob,
    encrypt_blob,
    shred,
    verify_ciphertext_hash,
)
from novafabric.trust.novaseal.signing_backend import (
    KeyWrappingBackend,
    LocalSigningBackend,
    MockKmsBackend,
)

PLAINTEXT = b"capsule payload \x00\x01\xff binary-safe"


@pytest.fixture()
def backend() -> MockKmsBackend:
    return MockKmsBackend()


class _CloudKmsError(RuntimeError):
    """Stand-in for a cloud-SDK unwrap failure (botocore ClientError / Azure / GCP)."""


class _FlakyCloudBackend:
    """A wrap-capable backend whose unwrap raises a non-InvalidTag SDK-style error.

    Mirrors the real AWS/Azure/GCP wrapping backends, which raise their own SDK
    exceptions (not cryptography.InvalidTag) when a KMS Decrypt fails.
    """

    def __init__(self) -> None:
        self._inner = MockKmsBackend()

    def wrap_key(self, plaintext_key: bytes) -> bytes:
        return self._inner.wrap_key(plaintext_key)

    def unwrap_key(self, wrapped_key: bytes) -> bytes:
        raise _CloudKmsError("KMS Decrypt failed: InvalidCiphertextException")

    def kek_ref(self) -> str:
        return "flaky-cloud-kms:test"


def test_cloud_backend_unwrap_failure_becomes_dek_unwrap_error() -> None:
    # Security-review regression (v0.74.0): a cloud KMS backend raises its own SDK
    # exception on unwrap failure, not cryptography.InvalidTag. decrypt_blob must
    # still surface a clean DekUnwrapError and never leak the raw SDK exception.
    blob = encrypt_blob(PLAINTEXT, backend=_FlakyCloudBackend())
    with pytest.raises(DekUnwrapError):
        decrypt_blob(blob, backend=_FlakyCloudBackend())


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_encrypt_decrypt_round_trip(self, backend: MockKmsBackend) -> None:
        blob = encrypt_blob(PLAINTEXT, backend=backend)
        assert decrypt_blob(blob, backend=backend) == PLAINTEXT

    def test_empty_plaintext_round_trip(self, backend: MockKmsBackend) -> None:
        blob = encrypt_blob(b"", backend=backend)
        assert decrypt_blob(blob, backend=backend) == b""

    def test_blob_fields(self, backend: MockKmsBackend) -> None:
        blob = encrypt_blob(PLAINTEXT, backend=backend)
        assert blob.algo == "AES-256-GCM"
        assert len(blob.nonce) == 12
        assert blob.wrapped_dek is not None
        assert blob.kek_ref.startswith("mock-kms:")
        assert blob.shredded is False
        # ciphertext is not the plaintext
        assert blob.ciphertext != PLAINTEXT

    def test_wrong_backend_kek_fails_unwrap(self, backend: MockKmsBackend) -> None:
        blob = encrypt_blob(PLAINTEXT, backend=backend)
        other = MockKmsBackend()  # different random KEK
        with pytest.raises(DekUnwrapError):
            decrypt_blob(blob, backend=other)


# ---------------------------------------------------------------------------
# Unique per-object DEK
# ---------------------------------------------------------------------------


class TestUniqueDek:
    def test_same_plaintext_encrypts_differently(self, backend: MockKmsBackend) -> None:
        a = encrypt_blob(PLAINTEXT, backend=backend)
        b = encrypt_blob(PLAINTEXT, backend=backend)
        assert a.ciphertext != b.ciphertext
        assert a.wrapped_dek != b.wrapped_dek
        assert a.nonce != b.nonce
        assert a.content_sha256 != b.content_sha256
        # both still decrypt to the same plaintext
        assert decrypt_blob(a, backend=backend) == PLAINTEXT
        assert decrypt_blob(b, backend=backend) == PLAINTEXT


# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------


class TestTamper:
    def _tampered_ciphertext(self, blob: EncryptedBlob) -> EncryptedBlob:
        raw = bytearray(blob.ciphertext)
        raw[0] ^= 0xFF
        tampered = bytes(raw)
        return blob.model_copy(
            update={
                "ciphertext_b64": base64.b64encode(tampered).decode("ascii"),
                # keep content_sha256 consistent with the tampered ciphertext so
                # the keyless hash gate passes and GCM itself must catch it
                "content_sha256": hashlib.sha256(tampered).hexdigest(),
            }
        )

    def test_tampered_ciphertext_raises_gcm_auth_failure(self, backend: MockKmsBackend) -> None:
        blob = encrypt_blob(PLAINTEXT, backend=backend)
        with pytest.raises(BlobAuthenticationError):
            decrypt_blob(self._tampered_ciphertext(blob), backend=backend)

    def test_ciphertext_hash_mismatch_refuses_decrypt(self, backend: MockKmsBackend) -> None:
        blob = encrypt_blob(PLAINTEXT, backend=backend)
        raw = bytearray(blob.ciphertext)
        raw[-1] ^= 0x01
        bad = blob.model_copy(
            update={"ciphertext_b64": base64.b64encode(bytes(raw)).decode("ascii")}
        )
        with pytest.raises(CiphertextIntegrityError):
            decrypt_blob(bad, backend=backend)

    def test_tampered_wrapped_dek_raises_unwrap_error(self, backend: MockKmsBackend) -> None:
        blob = encrypt_blob(PLAINTEXT, backend=backend)
        assert blob.wrapped_dek is not None
        raw = bytearray(blob.wrapped_dek)
        raw[-1] ^= 0x01
        bad = blob.model_copy(
            update={"wrapped_dek_b64": base64.b64encode(bytes(raw)).decode("ascii")}
        )
        with pytest.raises(DekUnwrapError):
            decrypt_blob(bad, backend=backend)


# ---------------------------------------------------------------------------
# Hash over ciphertext — keyless verification
# ---------------------------------------------------------------------------


class TestHashOverCiphertext:
    def test_hash_verifiable_without_keys(self, backend: MockKmsBackend) -> None:
        blob = encrypt_blob(PLAINTEXT, backend=backend)
        # verification takes no backend and touches no key material
        assert verify_ciphertext_hash(blob) is True
        # and matches an independent recomputation over the ciphertext
        assert hashlib.sha256(blob.ciphertext).hexdigest() == blob.content_sha256
        # normative: the hash is NOT over the plaintext
        assert hashlib.sha256(PLAINTEXT).hexdigest() != blob.content_sha256

    def test_hash_detects_ciphertext_change(self, backend: MockKmsBackend) -> None:
        blob = encrypt_blob(PLAINTEXT, backend=backend)
        raw = bytearray(blob.ciphertext)
        raw[0] ^= 0x01
        bad = blob.model_copy(
            update={"ciphertext_b64": base64.b64encode(bytes(raw)).decode("ascii")}
        )
        assert verify_ciphertext_hash(bad) is False

    def test_shredded_blob_still_hash_verifies(self, backend: MockKmsBackend) -> None:
        blob = shred(encrypt_blob(PLAINTEXT, backend=backend))
        assert verify_ciphertext_hash(blob) is True


# ---------------------------------------------------------------------------
# Crypto-shred (ADR-0134 synergy)
# ---------------------------------------------------------------------------


class TestShred:
    def test_shredded_blob_cannot_decrypt(self, backend: MockKmsBackend) -> None:
        blob = shred(encrypt_blob(PLAINTEXT, backend=backend))
        assert blob.shredded is True
        assert blob.wrapped_dek is None
        with pytest.raises(ShreddedBlobError):
            decrypt_blob(blob, backend=backend)

    def test_shred_is_idempotent_and_preserves_ciphertext(self, backend: MockKmsBackend) -> None:
        blob = encrypt_blob(PLAINTEXT, backend=backend)
        once = shred(blob)
        twice = shred(once)
        assert once == twice
        assert once.ciphertext == blob.ciphertext
        assert once.content_sha256 == blob.content_sha256

    def test_shred_does_not_mutate_original(self, backend: MockKmsBackend) -> None:
        blob = encrypt_blob(PLAINTEXT, backend=backend)
        shred(blob)
        assert blob.shredded is False
        assert decrypt_blob(blob, backend=backend) == PLAINTEXT

    def test_shredded_blob_survives_json_serialization(self, backend: MockKmsBackend) -> None:
        blob = shred(encrypt_blob(PLAINTEXT, backend=backend))
        restored = EncryptedBlob.model_validate_json(blob.model_dump_json())
        assert restored == blob
        assert restored.shredded is True
        assert restored.wrapped_dek is None
        assert verify_ciphertext_hash(restored) is True
        with pytest.raises(ShreddedBlobError):
            decrypt_blob(restored, backend=backend)

    def test_live_blob_survives_json_serialization(self, backend: MockKmsBackend) -> None:
        blob = encrypt_blob(PLAINTEXT, backend=backend)
        restored = EncryptedBlob.model_validate_json(blob.model_dump_json())
        assert decrypt_blob(restored, backend=backend) == PLAINTEXT


# ---------------------------------------------------------------------------
# LocalSigningBackend KEK-file wrap capability
# ---------------------------------------------------------------------------


class TestLocalBackendWrap:
    def _backend(self, tmp_path: Path, kek: bytes | str) -> LocalSigningBackend:
        kek_path = tmp_path / "kek.bin"
        if isinstance(kek, str):
            kek_path.write_text(kek)
        else:
            kek_path.write_bytes(kek)
        # key/cert paths are unused by the wrap capability
        return LocalSigningBackend(tmp_path / "key.pem", tmp_path / "cert.pem", kek_path=kek_path)

    def test_wrap_unwrap_round_trip_raw_kek(self, tmp_path: Path) -> None:
        backend = self._backend(tmp_path, b"\x42" * 32)
        dek = b"\x07" * 32
        wrapped = backend.wrap_key(dek)
        assert wrapped != dek
        assert backend.unwrap_key(wrapped) == dek

    def test_wrap_unwrap_round_trip_hex_kek(self, tmp_path: Path) -> None:
        backend = self._backend(tmp_path, "ab" * 32 + "\n")
        dek = b"\x07" * 32
        assert backend.unwrap_key(backend.wrap_key(dek)) == dek

    def test_full_encrypt_decrypt_via_local_backend(self, tmp_path: Path) -> None:
        backend = self._backend(tmp_path, b"\x42" * 32)
        blob = encrypt_blob(PLAINTEXT, backend=backend)
        assert blob.kek_ref.startswith("local-kek:")
        assert decrypt_blob(blob, backend=backend) == PLAINTEXT

    def test_local_backend_without_kek_path_raises(self, tmp_path: Path) -> None:
        backend = LocalSigningBackend(tmp_path / "key.pem", tmp_path / "cert.pem")
        with pytest.raises(NotImplementedError, match="kek_path"):
            backend.wrap_key(b"\x00" * 32)

    def test_bad_kek_file_length_raises(self, tmp_path: Path) -> None:
        backend = self._backend(tmp_path, b"\x42" * 16)
        with pytest.raises(ValueError, match="32 raw bytes or 64 hex"):
            backend.wrap_key(b"\x00" * 32)

    def test_local_backend_satisfies_capability_protocol(self, tmp_path: Path) -> None:
        backend = self._backend(tmp_path, b"\x42" * 32)
        assert isinstance(backend, KeyWrappingBackend)
        assert isinstance(MockKmsBackend(), KeyWrappingBackend)


# ---------------------------------------------------------------------------
# Backends without the KMS capability
# ---------------------------------------------------------------------------


class _SignOnlyBackend:
    """A SigningBackend-shaped object with no wrap capability."""

    def sign_digest(self, digest: bytes) -> bytes:
        return b"sig"

    def get_cert_der(self) -> bytes:
        return b"cert"


class TestNoCapability:
    def test_encrypt_raises_not_implemented_with_clear_message(self) -> None:
        with pytest.raises(NotImplementedError, match="wrap_key") as excinfo:
            encrypt_blob(PLAINTEXT, backend=_SignOnlyBackend())  # type: ignore[arg-type]
        message = str(excinfo.value)
        assert "_SignOnlyBackend" in message
        assert "ADR-0185" in message

    def test_decrypt_raises_not_implemented(self, backend: MockKmsBackend) -> None:
        blob = encrypt_blob(PLAINTEXT, backend=backend)
        with pytest.raises(NotImplementedError, match="wrap_key"):
            decrypt_blob(blob, backend=_SignOnlyBackend())  # type: ignore[arg-type]

    def test_mock_kms_rejects_bad_kek_length(self) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            MockKmsBackend(kek=b"\x00" * 16)
