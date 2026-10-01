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
"""ADR-0290: envelope v2 (object-identity AAD) and fail-closed plaintext reads.

Properties, one test each where possible:

- new envelopes are v2 and bound to their object key: an envelope swapped
  onto another key, or with a tampered/downgraded header, is rejected;
- v1 envelopes (the committed golden fixture, written without AAD) still
  decrypt, and the adapter flags each such read;
- non-envelope bytes outside the chain-log namespace are refused with
  ``PlaintextObjectRefusedError`` unless the legacy opt-in is set (argument
  or ``NOVA_OBJECT_STORE_ALLOW_PLAINTEXT_READS``);
- KEK rotation by DEK re-wrap keeps a bound envelope readable (the AAD does
  not bind ``kek_ref``), and tenant-KEK revocation still fails closed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from novafabric.object_capsule_store.backend_router import (
    ENV_ALLOW_PLAINTEXT_READS,
    ENV_ENCRYPTION,
    ENV_KEK_PATH,
    InMemoryWormAdapter,
    make_adapter,
)
from novafabric.object_capsule_store.cas import compute_sha256
from novafabric.object_capsule_store.encryption_wrapper import (
    CHAIN_LOG_PREFIX,
    EncryptingAdapter,
    PlaintextObjectRefusedError,
)
from novafabric.object_capsule_store.worm.base import WormAdapter
from novafabric.trust.envelope_encryption import (
    ENVELOPE_VERSION_BOUND,
    ENVELOPE_VERSION_LEGACY,
    BlobAuthenticationError,
    DekUnwrapError,
    EncryptedBlob,
    EnvelopeBindingError,
    EnvelopeEncryptionError,
    decrypt_blob,
    encrypt_blob,
    envelope_aad,
)
from novafabric.trust.novaseal.signing_backend import LocalSigningBackend, MockKmsBackend
from novafabric.trust.tenant_keys import TenantKeyRegistry

FIXTURE = Path(__file__).parents[1] / "fixtures" / "encryption" / "envelope-v1-unbound.json"
FIXTURE_KEK = bytes(range(32))
FIXTURE_PLAINTEXT = b"pre-ADR-0290 capsule payload"

KEY_A = "capsules/acme/aa/" + "a" * 64 + "/data.zst"
KEY_B = "capsules/acme/bb/" + "b" * 64 + "/data.zst"


@pytest.fixture
def kms() -> MockKmsBackend:
    return MockKmsBackend()


@pytest.fixture
def inner() -> InMemoryWormAdapter:
    return InMemoryWormAdapter()


@pytest.fixture
def adapter(inner: InMemoryWormAdapter, kms: MockKmsBackend) -> EncryptingAdapter:
    return EncryptingAdapter(inner, kms)


def _put(adapter: EncryptingAdapter, key: str, data: bytes) -> None:
    adapter.put_object(key, data, compute_sha256(data), retention_days=1)


def _store_raw(inner: WormAdapter, key: str, raw: bytes) -> None:
    inner.put_object(key, raw, compute_sha256(raw), retention_days=1)


# ---------------------------------------------------------------------------
# Crypto layer — v2 binding
# ---------------------------------------------------------------------------


class TestBoundEnvelope:
    def test_object_id_produces_v2_envelope(self, kms: MockKmsBackend) -> None:
        blob = encrypt_blob(b"x", backend=kms, object_id=KEY_A)
        assert blob.envelope_version == ENVELOPE_VERSION_BOUND
        assert blob.is_bound
        assert decrypt_blob(blob, backend=kms, object_id=KEY_A) == b"x"

    def test_no_object_id_keeps_legacy_v1_for_api_compat(self, kms: MockKmsBackend) -> None:
        blob = encrypt_blob(b"x", backend=kms)
        assert blob.envelope_version == ENVELOPE_VERSION_LEGACY
        assert not blob.is_bound
        assert decrypt_blob(blob, backend=kms) == b"x"

    def test_wrong_object_id_rejected(self, kms: MockKmsBackend) -> None:
        blob = encrypt_blob(b"x", backend=kms, object_id=KEY_A)
        with pytest.raises(BlobAuthenticationError, match="ADR-0290"):
            decrypt_blob(blob, backend=kms, object_id=KEY_B)

    def test_missing_object_id_rejected(self, kms: MockKmsBackend) -> None:
        blob = encrypt_blob(b"x", backend=kms, object_id=KEY_A)
        with pytest.raises(EnvelopeBindingError):
            decrypt_blob(blob, backend=kms)

    def test_downgrade_to_v1_header_rejected(self, kms: MockKmsBackend) -> None:
        """Relabelling a bound envelope as v1 must not strip the binding."""
        blob = encrypt_blob(b"x", backend=kms, object_id=KEY_A)
        downgraded = blob.model_copy(update={"envelope_version": ENVELOPE_VERSION_LEGACY})
        with pytest.raises(BlobAuthenticationError):
            decrypt_blob(downgraded, backend=kms, object_id=KEY_A)

    def test_empty_object_id_rejected(self) -> None:
        with pytest.raises(EnvelopeBindingError):
            envelope_aad("")

    def test_aad_is_domain_separated(self) -> None:
        assert envelope_aad(KEY_A).startswith(b"novafabric/envelope-aad/v2\x00")
        assert envelope_aad(KEY_A) != envelope_aad(KEY_B)

    def test_v2_round_trips_through_json(self, kms: MockKmsBackend) -> None:
        blob = encrypt_blob(b"x", backend=kms, object_id=KEY_A)
        again = EncryptedBlob.model_validate_json(blob.model_dump_json())
        assert again.envelope_version == ENVELOPE_VERSION_BOUND
        assert decrypt_blob(again, backend=kms, object_id=KEY_A) == b"x"


# ---------------------------------------------------------------------------
# Store — tamper / swap
# ---------------------------------------------------------------------------


class TestStoreBinding:
    def test_new_writes_are_v2(self, adapter: EncryptingAdapter, inner: InMemoryWormAdapter) -> None:
        _put(adapter, KEY_A, b"secret-a")
        stored = json.loads(inner.get_object(KEY_A))
        assert stored["envelope_version"] == ENVELOPE_VERSION_BOUND
        assert adapter.get_object(KEY_A) == b"secret-a"
        assert adapter.legacy_envelope_reads == 0

    def test_swapped_envelope_rejected(
        self, adapter: EncryptingAdapter, inner: InMemoryWormAdapter
    ) -> None:
        """An attacker with inner-store write access copies A's envelope to key B."""
        _put(adapter, KEY_A, b"secret-a")
        _store_raw(inner, KEY_B, inner.get_object(KEY_A))
        with pytest.raises(BlobAuthenticationError):
            adapter.get_object(KEY_B)

    def test_tampered_ciphertext_rejected(
        self, adapter: EncryptingAdapter, inner: InMemoryWormAdapter
    ) -> None:
        _put(adapter, KEY_A, b"secret-a")
        env = json.loads(inner.get_object(KEY_A))
        blob = EncryptedBlob.model_validate(env)
        flipped = bytearray(blob.ciphertext)
        flipped[0] ^= 0x01
        import base64
        import hashlib

        env["ciphertext_b64"] = base64.b64encode(bytes(flipped)).decode()
        env["content_sha256"] = hashlib.sha256(bytes(flipped)).hexdigest()
        _store_raw(inner, KEY_B, json.dumps(env).encode())
        with pytest.raises(EnvelopeEncryptionError):
            adapter.get_object(KEY_B)


# ---------------------------------------------------------------------------
# Backward compatibility — v1 envelopes
# ---------------------------------------------------------------------------


class TestLegacyV1:
    def test_golden_v1_fixture_has_no_version_field(self) -> None:
        raw = json.loads(FIXTURE.read_text())
        assert "envelope_version" not in raw

    def test_golden_v1_fixture_decrypts(self) -> None:
        blob = EncryptedBlob.model_validate_json(FIXTURE.read_text())
        assert blob.envelope_version == ENVELOPE_VERSION_LEGACY
        kms = MockKmsBackend(FIXTURE_KEK)
        assert decrypt_blob(blob, backend=kms) == FIXTURE_PLAINTEXT
        # object_id is ignored for a v1 envelope — any caller-supplied id works.
        assert decrypt_blob(blob, backend=kms, object_id=KEY_A) == FIXTURE_PLAINTEXT

    def test_v1_read_through_store_is_flagged(
        self, inner: InMemoryWormAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = EncryptingAdapter(inner, MockKmsBackend(FIXTURE_KEK))
        _store_raw(inner, KEY_A, FIXTURE.read_bytes())
        with caplog.at_level(logging.WARNING):
            assert adapter.get_object(KEY_A) == FIXTURE_PLAINTEXT
        assert adapter.legacy_envelope_reads == 1
        assert "legacy unbound (v1) envelope" in caplog.text


# ---------------------------------------------------------------------------
# Fail-closed plaintext reads
# ---------------------------------------------------------------------------


class TestPlaintextRefusal:
    def test_plaintext_refused_by_default(
        self, adapter: EncryptingAdapter, inner: InMemoryWormAdapter
    ) -> None:
        _store_raw(inner, KEY_A, b"substituted plaintext")
        with pytest.raises(PlaintextObjectRefusedError, match="ALLOW_PLAINTEXT_READS") as ei:
            adapter.get_object(KEY_A)
        assert ei.value.key == KEY_A
        assert isinstance(ei.value, EnvelopeEncryptionError)

    def test_non_envelope_json_refused_by_default(
        self, adapter: EncryptingAdapter, inner: InMemoryWormAdapter
    ) -> None:
        _store_raw(inner, KEY_A, b'{"algo": "AES-256-GCM"}')
        with pytest.raises(PlaintextObjectRefusedError):
            adapter.get_object(KEY_A)

    def test_chain_log_namespace_still_passes_through(
        self, adapter: EncryptingAdapter
    ) -> None:
        key = f"{CHAIN_LOG_PREFIX}acme/run-1/0000000000.json"
        adapter.put_log_object(key, b'{"version": 0}')
        assert adapter.get_object(key) == b'{"version": 0}'

    def test_opt_in_returns_plaintext_and_counts(
        self, inner: InMemoryWormAdapter, kms: MockKmsBackend, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = EncryptingAdapter(inner, kms, allow_plaintext_reads=True)
        _store_raw(inner, KEY_A, b"pre-encryption object")
        with caplog.at_level(logging.WARNING):
            assert adapter.get_object(KEY_A) == b"pre-encryption object"
        assert adapter.plaintext_reads == 1
        assert "legacy plaintext-read opt-in" in caplog.text

    def test_env_default_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        kek = tmp_path / "kek.bin"
        kek.write_bytes(b"\x07" * 32)
        monkeypatch.setenv(ENV_ENCRYPTION, "1")
        monkeypatch.setenv(ENV_KEK_PATH, str(kek))
        monkeypatch.delenv(ENV_ALLOW_PLAINTEXT_READS, raising=False)
        adapter = make_adapter("local")
        assert isinstance(adapter, EncryptingAdapter)
        _store_raw(adapter._inner, KEY_A, b"raw")  # noqa: SLF001
        with pytest.raises(PlaintextObjectRefusedError):
            adapter.get_object(KEY_A)

    def test_env_opt_in_allows(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        kek = tmp_path / "kek.bin"
        kek.write_bytes(b"\x07" * 32)
        monkeypatch.setenv(ENV_ENCRYPTION, "1")
        monkeypatch.setenv(ENV_KEK_PATH, str(kek))
        monkeypatch.setenv(ENV_ALLOW_PLAINTEXT_READS, "1")
        adapter = make_adapter("local")
        assert isinstance(adapter, EncryptingAdapter)
        _store_raw(adapter._inner, KEY_A, b"raw")  # noqa: SLF001
        assert adapter.get_object(KEY_A) == b"raw"
        # Encrypted writes are still bound v2 under the opt-in.
        _put(adapter, KEY_B, b"new")
        assert adapter.get_object(KEY_B) == b"new"
        assert adapter.legacy_envelope_reads == 0


# ---------------------------------------------------------------------------
# Key rotation interaction
# ---------------------------------------------------------------------------


def _rewrap(blob: EncryptedBlob, old: MockKmsBackend, new: MockKmsBackend) -> EncryptedBlob:
    """Re-wrap the DEK under a new KEK without touching nonce/ciphertext."""
    import base64

    assert blob.wrapped_dek is not None
    dek = old.unwrap_key(blob.wrapped_dek)
    return blob.model_copy(
        update={
            "wrapped_dek_b64": base64.b64encode(new.wrap_key(dek)).decode(),
            "kek_ref": new.kek_ref(),
        }
    )


class TestRotationInteraction:
    def test_rewrap_keeps_bound_envelope_readable(self) -> None:
        """The AAD binds the object key only, so a KEK re-wrap is metadata-only."""
        old, new = MockKmsBackend(), MockKmsBackend()
        blob = encrypt_blob(b"payload", backend=old, object_id=KEY_A)
        rotated = _rewrap(blob, old, new)
        assert rotated.content_sha256 == blob.content_sha256  # ciphertext untouched
        assert decrypt_blob(rotated, backend=new, object_id=KEY_A) == b"payload"
        with pytest.raises(DekUnwrapError):
            decrypt_blob(rotated, backend=old, object_id=KEY_A)

    def test_rewrap_does_not_unbind(self) -> None:
        old, new = MockKmsBackend(), MockKmsBackend()
        rotated = _rewrap(encrypt_blob(b"p", backend=old, object_id=KEY_A), old, new)
        with pytest.raises(BlobAuthenticationError):
            decrypt_blob(rotated, backend=new, object_id=KEY_B)

    def test_tenant_kek_revocation_still_fails_closed_for_v2(self, tmp_path: Path) -> None:
        default_kek = tmp_path / "default.kek"
        default_kek.write_bytes(b"\x01" * 32)
        kek_dir = tmp_path / "tenants"
        kek_dir.mkdir()
        (kek_dir / "acme.kek").write_bytes(b"\x02" * 32)
        default = LocalSigningBackend(default_kek, default_kek, kek_path=default_kek)
        inner = InMemoryWormAdapter()
        adapter = EncryptingAdapter(
            inner, default, tenant_keys=TenantKeyRegistry(default, kek_dir)
        )
        _put(adapter, KEY_A, b"acme-secret")
        stored = json.loads(inner.get_object(KEY_A))
        assert stored["tenant_key_id"] == "acme"
        assert stored["envelope_version"] == ENVELOPE_VERSION_BOUND
        assert adapter.get_object(KEY_A) == b"acme-secret"
        (kek_dir / "acme.kek").unlink()
        with pytest.raises(DekUnwrapError, match="acme"):
            adapter.get_object(KEY_A)
