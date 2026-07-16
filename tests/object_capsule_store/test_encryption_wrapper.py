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
"""Normative tests for the ADR-0185 second slice: opt-in store wiring.

Covers the EncryptingAdapter over the WORM adapter interface:

- put/get round-trip through a local (in-memory) adapter
- encrypt-before-WORM-write: stored bytes are ciphertext (no plaintext marker)
- CAS/hash address computed over the STORED (encrypted) bytes, verifiable
  WITHOUT the KEK
- mixed store: raw objects written before encryption was enabled still read
- wrong KEK -> DekUnwrapError; shredded envelope -> ShreddedBlobError
- opt-in only: env absent -> make_adapter returns the bare adapter unchanged
- WORM semantics preserved: overwrite-in-place still rejected through the
  wrapper (conformance-style check, cap-003 overwrite test analogue)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novafabric.object_capsule_store.backend_router import (
    ENV_ENCRYPTION,
    ENV_KEK_PATH,
    InMemoryWormAdapter,
    make_adapter,
)
from novafabric.object_capsule_store.cas import compute_sha256
from novafabric.object_capsule_store.encryption_wrapper import EncryptingAdapter
from novafabric.object_capsule_store.exceptions import CASMismatchError
from novafabric.object_capsule_store.worm.base import ConditionalPutConflict, WormAdapter
from novafabric.trust.envelope_encryption import (
    DekUnwrapError,
    EncryptedBlob,
    ShreddedBlobError,
    shred,
    verify_ciphertext_hash,
)
from novafabric.trust.novaseal.signing_backend import MockKmsBackend

PLAINTEXT_MARKER = b"NOVA-SECRET-CAPSULE-PAYLOAD"
PLAINTEXT = b"header \x00\x01\xff " + PLAINTEXT_MARKER + b" trailer"


@pytest.fixture
def inner() -> InMemoryWormAdapter:
    return InMemoryWormAdapter()


@pytest.fixture
def kms() -> MockKmsBackend:
    return MockKmsBackend()


@pytest.fixture
def wrapper(inner: InMemoryWormAdapter, kms: MockKmsBackend) -> EncryptingAdapter:
    return EncryptingAdapter(inner, kms)


def _put(wrapper: EncryptingAdapter, key: str, data: bytes = PLAINTEXT):
    return wrapper.put_object(key, data, compute_sha256(data), retention_days=365)


# ---------------------------------------------------------------------------
# Round-trip + interface compliance
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_is_worm_adapter(self, wrapper: EncryptingAdapter) -> None:
        assert isinstance(wrapper, WormAdapter)

    def test_put_get_round_trip(self, wrapper: EncryptingAdapter) -> None:
        result = _put(wrapper, "capsules/t1/ab/key1/data.zst")
        assert result.key == "capsules/t1/ab/key1/data.zst"
        assert wrapper.get_object("capsules/t1/ab/key1/data.zst") == PLAINTEXT

    def test_empty_payload_round_trip(self, wrapper: EncryptingAdapter) -> None:
        _put(wrapper, "k-empty", b"")
        assert wrapper.get_object("k-empty") == b""

    def test_apply_identical_round_trip(self, wrapper: EncryptingAdapter) -> None:
        data_a, data_b = PLAINTEXT, b'{"novaseal": "sibling"}'
        ra, rb = wrapper.apply_identical(
            "key_a", data_a, compute_sha256(data_a),
            "key_b", data_b, compute_sha256(data_b),
            retention_days=365,
        )
        assert (ra.key, rb.key) == ("key_a", "key_b")
        assert wrapper.get_object("key_a") == data_a
        assert wrapper.get_object("key_b") == data_b

    def test_caller_cas_gate_still_fails_fast(self, wrapper: EncryptingAdapter) -> None:
        """FR-14: plaintext CAS mismatch raises before any encryption/storage."""
        with pytest.raises(CASMismatchError):
            wrapper.put_object("k", PLAINTEXT, "0" * 64, retention_days=365)

    def test_same_plaintext_stores_distinct_ciphertexts(
        self, wrapper: EncryptingAdapter, inner: InMemoryWormAdapter
    ) -> None:
        """Fresh per-object DEK: identical payloads never share stored bytes."""
        _put(wrapper, "k1")
        _put(wrapper, "k2")
        assert inner.get_object("k1") != inner.get_object("k2")


# ---------------------------------------------------------------------------
# Encrypt-before-WORM-write (normative, ADR-0185)
# ---------------------------------------------------------------------------


class TestCiphertextAtRest:
    def test_stored_bytes_are_ciphertext(
        self, wrapper: EncryptingAdapter, inner: InMemoryWormAdapter
    ) -> None:
        """The bytes handed to the WORM adapter contain no plaintext marker."""
        _put(wrapper, "k")
        stored = inner.get_object("k")
        assert stored != PLAINTEXT
        assert PLAINTEXT_MARKER not in stored

    def test_stored_bytes_are_a_serialized_envelope(
        self, wrapper: EncryptingAdapter, inner: InMemoryWormAdapter
    ) -> None:
        _put(wrapper, "k")
        blob = EncryptedBlob.model_validate_json(inner.get_object("k"))
        assert blob.algo == "AES-256-GCM"
        assert blob.shredded is False
        assert PLAINTEXT_MARKER not in blob.ciphertext

    def test_cas_address_over_stored_bytes_verifies_without_kek(
        self, wrapper: EncryptingAdapter, inner: InMemoryWormAdapter
    ) -> None:
        """Normative: the hash the WORM backend sees is computed over the
        STORED (encrypted) bytes and verifies with zero key material."""
        result = _put(wrapper, "k")
        stored = inner.get_object("k")
        stored_sha = compute_sha256(stored)
        # InMemoryWormAdapter validates + echoes the sha it was handed:
        # a plaintext-derived sha would have raised CASMismatchError already.
        assert result.confirmation_token == f"etag-{stored_sha[:8]}"
        # ...and it is NOT the plaintext hash.
        assert stored_sha != compute_sha256(PLAINTEXT)
        # Envelope-internal ciphertext hash also verifies without the KEK.
        blob = EncryptedBlob.model_validate_json(stored)
        assert verify_ciphertext_hash(blob) is True

    def test_chain_log_objects_pass_through_unencrypted(
        self, wrapper: EncryptingAdapter, inner: InMemoryWormAdapter
    ) -> None:
        """Chain-log objects are integrity metadata, excluded from encryption."""
        wrapper.put_log_object("_capsule_log/t1/r1/000001.json", b'{"commit": 1}')
        assert inner.get_object("_capsule_log/t1/r1/000001.json") == b'{"commit": 1}'
        wrapper.put_log_object_if_absent("_capsule_log/t1/r1/000002.json", b'{"commit": 2}')
        with pytest.raises(ConditionalPutConflict):
            wrapper.put_log_object_if_absent("_capsule_log/t1/r1/000002.json", b"dup")


# ---------------------------------------------------------------------------
# Mixed store: objects stored before encryption was enabled
# ---------------------------------------------------------------------------


class TestMixedStore:
    def test_preexisting_raw_object_reads_unchanged(
        self, wrapper: EncryptingAdapter, inner: InMemoryWormAdapter
    ) -> None:
        raw = b"legacy plaintext capsule bytes \x00\xff"
        inner.put_object("old-key", raw, compute_sha256(raw), retention_days=365)
        assert wrapper.get_object("old-key") == raw

    def test_preexisting_json_without_envelope_marker_reads_unchanged(
        self, wrapper: EncryptingAdapter, inner: InMemoryWormAdapter
    ) -> None:
        """JSON that is not an EncryptedBlob envelope must not be 'decrypted'."""
        raw = json.dumps({"algo": "AES-256-GCM", "not_an": "envelope"}).encode()
        inner.put_log_object("old-json", raw)
        assert wrapper.get_object("old-json") == raw

    def test_namespace_ops_delegate(
        self, wrapper: EncryptingAdapter, inner: InMemoryWormAdapter
    ) -> None:
        _put(wrapper, "prefix/a")
        inner.put_log_object("prefix/b", b"raw")
        assert wrapper.list_objects("prefix/") == ["prefix/a", "prefix/b"]
        assert sorted(wrapper.iter_objects("prefix/")) == ["prefix/a", "prefix/b"]
        assert wrapper.object_exists("prefix/a") is True
        assert wrapper.object_exists("prefix/zzz") is False


# ---------------------------------------------------------------------------
# Failure modes: wrong KEK, crypto-shred
# ---------------------------------------------------------------------------


class TestFailureModes:
    def test_wrong_kek_raises_dek_unwrap_error(
        self, wrapper: EncryptingAdapter, inner: InMemoryWormAdapter
    ) -> None:
        _put(wrapper, "k")
        other = EncryptingAdapter(inner, MockKmsBackend())  # different random KEK
        with pytest.raises(DekUnwrapError):
            other.get_object("k")

    def test_shredded_envelope_raises_shredded_blob_error(
        self, wrapper: EncryptingAdapter, inner: InMemoryWormAdapter
    ) -> None:
        """Crypto-shred (ADR-0134): reads of a shredded object fail closed."""
        _put(wrapper, "k")
        blob = EncryptedBlob.model_validate_json(inner.get_object("k"))
        shredded = shred(blob)
        inner.put_log_object("k-shredded", shredded.model_dump_json().encode())
        with pytest.raises(ShreddedBlobError):
            wrapper.get_object("k-shredded")
        # Ciphertext stays intact and hash-verifiable without any key material.
        restored = EncryptedBlob.model_validate_json(inner.get_object("k-shredded"))
        assert verify_ciphertext_hash(restored) is True


# ---------------------------------------------------------------------------
# WORM semantics preserved through the wrapper (conformance-style, cap-003)
# ---------------------------------------------------------------------------


class _LockedInMemoryAdapter(InMemoryWormAdapter):
    """In-memory adapter that enforces overwrite-in-place rejection for locked
    objects — the cap-003 ``overwrite_in_place_rejected_for_locked_object``
    conformance behavior, so the wrapper can be checked against it."""

    def put_object(self, key, data, sha256_hex, retention_days, content_type="application/octet-stream"):  # type: ignore[no-untyped-def]
        if self.object_exists(key):
            raise PermissionError(
                f"WORM COMPLIANCE: overwrite-in-place rejected for locked key {key!r}"
            )
        return super().put_object(key, data, sha256_hex, retention_days, content_type)


class TestWormSemanticsPreserved:
    def test_overwrite_in_place_rejected_through_wrapper(
        self, kms: MockKmsBackend
    ) -> None:
        locked = _LockedInMemoryAdapter()
        wrapper = EncryptingAdapter(locked, kms)
        _put(wrapper, "k")
        original_stored = locked.get_object("k")
        with pytest.raises(PermissionError, match="WORM COMPLIANCE"):
            _put(wrapper, "k", b"attacker replacement payload")
        # Stored ciphertext is untouched and the original still decrypts.
        assert locked.get_object("k") == original_stored
        assert wrapper.get_object("k") == PLAINTEXT

    def test_delete_object_error_propagates(self, kms: MockKmsBackend) -> None:
        class _NoDeleteAdapter(InMemoryWormAdapter):
            def delete_object(self, key: str) -> None:
                raise PermissionError("WORM COMPLIANCE: delete rejected")

        wrapper = EncryptingAdapter(_NoDeleteAdapter(), kms)
        with pytest.raises(PermissionError):
            wrapper.delete_object("k")


# ---------------------------------------------------------------------------
# Opt-in configuration via make_adapter (env plumbing)
# ---------------------------------------------------------------------------


@pytest.fixture
def kek_file(tmp_path: Path) -> Path:
    kek_path = tmp_path / "kek.bin"
    kek_path.write_bytes(b"\x42" * 32)
    return kek_path


class TestOptInConfig:
    def test_env_absent_returns_bare_adapter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: without opt-in env, behavior is byte-for-byte unchanged."""
        monkeypatch.delenv(ENV_ENCRYPTION, raising=False)
        monkeypatch.delenv(ENV_KEK_PATH, raising=False)
        adapter = make_adapter("local")
        assert isinstance(adapter, InMemoryWormAdapter)
        assert not isinstance(adapter, EncryptingAdapter)

    def test_env_falsy_returns_bare_adapter(
        self, monkeypatch: pytest.MonkeyPatch, kek_file: Path
    ) -> None:
        monkeypatch.setenv(ENV_ENCRYPTION, "0")
        monkeypatch.setenv(ENV_KEK_PATH, str(kek_file))
        assert not isinstance(make_adapter("local"), EncryptingAdapter)

    def test_env_opt_in_wraps_and_round_trips(
        self, monkeypatch: pytest.MonkeyPatch, kek_file: Path
    ) -> None:
        monkeypatch.setenv(ENV_ENCRYPTION, "1")
        monkeypatch.setenv(ENV_KEK_PATH, str(kek_file))
        adapter = make_adapter("local")
        assert isinstance(adapter, EncryptingAdapter)
        adapter.put_object("k", PLAINTEXT, compute_sha256(PLAINTEXT), retention_days=1)
        assert adapter.get_object("k") == PLAINTEXT

    def test_env_opt_in_kek_survives_across_adapters(
        self, monkeypatch: pytest.MonkeyPatch, kek_file: Path
    ) -> None:
        """Two adapters over the same KEK file decrypt each other's writes
        (the KEK is the file, not per-process state) — but the underlying
        local stores are independent, so share the inner adapter here."""
        monkeypatch.setenv(ENV_ENCRYPTION, "1")
        monkeypatch.setenv(ENV_KEK_PATH, str(kek_file))
        a = make_adapter("local")
        assert isinstance(a, EncryptingAdapter)
        a.put_object("k", PLAINTEXT, compute_sha256(PLAINTEXT), retention_days=1)
        b = make_adapter("local")
        assert isinstance(b, EncryptingAdapter)
        # decrypt a's envelope with b's independently-constructed backend
        stored = a._inner.get_object("k")  # noqa: SLF001
        b._inner.put_log_object("k", stored)  # noqa: SLF001
        assert b.get_object("k") == PLAINTEXT

    def test_env_opt_in_without_kek_path_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_ENCRYPTION, "1")
        monkeypatch.delenv(ENV_KEK_PATH, raising=False)
        with pytest.raises(ValueError, match="NOVA_OBJECT_STORE_KEK_PATH"):
            make_adapter("local")
