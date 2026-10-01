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
"""ADR-0295: digest-pinned legacy-object inventory and v1 strict mode.

- the inventory pins each pre-encryption object (plaintext or v1 envelope) to
  the SHA-256 of its stored bytes; listed + unchanged is admitted, listed +
  substituted and unlisted are refused;
- strict mode refuses unbound v1 envelopes the inventory does not pin —
  including an old v1 envelope swapped onto a new key;
- defaults are unchanged (no inventory, no strict mode ⇒ ADR-0290 behaviour);
- env wiring fails closed on an unreadable, malformed or pin-mismatched file.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from novafabric.object_capsule_store.backend_router import (
    ENV_ALLOW_PLAINTEXT_READS,
    ENV_ENCRYPTION,
    ENV_KEK_PATH,
    ENV_LEGACY_INVENTORY,
    ENV_LEGACY_INVENTORY_SHA256,
    ENV_REFUSE_V1_ENVELOPES,
    InMemoryWormAdapter,
    make_adapter,
)
from novafabric.object_capsule_store.cas import compute_sha256
from novafabric.object_capsule_store.encryption_wrapper import (
    CHAIN_LOG_PREFIX,
    EncryptingAdapter,
    LegacyEnvelopeRefusedError,
    PlaintextObjectRefusedError,
)
from novafabric.object_capsule_store.legacy_inventory import (
    INVENTORY_FORMAT,
    LegacyInventory,
    LegacyInventoryError,
    build_legacy_inventory,
    load_legacy_inventory,
)
from novafabric.object_capsule_store.worm.base import WormAdapter
from novafabric.trust.envelope_encryption import EnvelopeEncryptionError
from novafabric.trust.novaseal.signing_backend import MockKmsBackend

FIXTURE = Path(__file__).parents[1] / "fixtures" / "encryption" / "envelope-v1-unbound.json"
FIXTURE_KEK = bytes(range(32))
FIXTURE_PLAINTEXT = b"pre-ADR-0290 capsule payload"

KEY_PLAIN = "capsules/acme/aa/" + "a" * 64 + "/data.zst"
KEY_V1 = "capsules/acme/bb/" + "b" * 64 + "/data.zst"
KEY_V2 = "capsules/acme/cc/" + "c" * 64 + "/data.zst"
KEY_NEW = "capsules/acme/dd/" + "d" * 64 + "/data.zst"
KEY_LOG = f"{CHAIN_LOG_PREFIX}acme/run-1/0000000000.json"


def _store_raw(inner: WormAdapter, key: str, raw: bytes) -> None:
    inner.put_object(key, raw, compute_sha256(raw), retention_days=1)


@pytest.fixture
def kms() -> MockKmsBackend:
    return MockKmsBackend(FIXTURE_KEK)


@pytest.fixture
def legacy_store(kms: MockKmsBackend) -> InMemoryWormAdapter:
    """A store at cut-over: one plaintext object, one v1, one v2, one chain-log."""
    inner = InMemoryWormAdapter()
    _store_raw(inner, KEY_PLAIN, b"pre-encryption payload")
    _store_raw(inner, KEY_V1, FIXTURE.read_bytes())
    EncryptingAdapter(inner, kms).put_object(
        KEY_V2, b"bound", compute_sha256(b"bound"), retention_days=1
    )
    inner.put_log_object(KEY_LOG, b'{"version": 0}')
    return inner


class TestBuild:
    def test_build_classifies_objects(self, legacy_store: InMemoryWormAdapter) -> None:
        inv = build_legacy_inventory(legacy_store)
        assert set(inv.objects) == {KEY_PLAIN, KEY_V1}
        assert inv.objects[KEY_PLAIN] == hashlib.sha256(b"pre-encryption payload").hexdigest()
        assert inv.objects[KEY_V1] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()

    def test_build_refuses_wrapped_adapter(
        self, legacy_store: InMemoryWormAdapter, kms: MockKmsBackend
    ) -> None:
        with pytest.raises(LegacyInventoryError, match="bare inner adapter"):
            build_legacy_inventory(EncryptingAdapter(legacy_store, kms))

    def test_round_trip_through_file(
        self, legacy_store: InMemoryWormAdapter, tmp_path: Path
    ) -> None:
        inv = build_legacy_inventory(legacy_store)
        path = tmp_path / "inventory.json"
        path.write_text(inv.to_json())
        again = load_legacy_inventory(
            path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest()
        )
        assert dict(again.objects) == dict(inv.objects)
        assert len(again) == 2 and KEY_PLAIN in again


class TestPlaintextAdmission:
    def test_listed_unchanged_plaintext_admitted(
        self, legacy_store: InMemoryWormAdapter, kms: MockKmsBackend
    ) -> None:
        adapter = EncryptingAdapter(
            legacy_store, kms, legacy_inventory=build_legacy_inventory(legacy_store)
        )
        assert adapter.get_object(KEY_PLAIN) == b"pre-encryption payload"
        assert adapter.read_counters() == {
            "legacy_envelope_reads": 0,
            "plaintext_reads": 1,
            "inventory_reads": 1,
            "legacy_refusals": 0,
        }

    def test_listed_substituted_plaintext_refused(
        self, kms: MockKmsBackend
    ) -> None:
        inner = InMemoryWormAdapter()
        inv = LegacyInventory(
            objects={KEY_PLAIN: hashlib.sha256(b"original").hexdigest()}
        )
        _store_raw(inner, KEY_PLAIN, b"substituted")
        adapter = EncryptingAdapter(inner, kms, legacy_inventory=inv)
        with pytest.raises(PlaintextObjectRefusedError, match="ADR-0295"):
            adapter.get_object(KEY_PLAIN)
        assert adapter.legacy_refusals == 1
        assert adapter.inventory_reads == 0

    def test_unlisted_plaintext_refused(
        self, legacy_store: InMemoryWormAdapter, kms: MockKmsBackend
    ) -> None:
        adapter = EncryptingAdapter(
            legacy_store, kms, legacy_inventory=build_legacy_inventory(legacy_store)
        )
        _store_raw(legacy_store, KEY_NEW, b"plaintext written after cut-over")
        with pytest.raises(PlaintextObjectRefusedError):
            adapter.get_object(KEY_NEW)

    def test_global_opt_in_still_wins(
        self, kms: MockKmsBackend
    ) -> None:
        inner = InMemoryWormAdapter()
        _store_raw(inner, KEY_NEW, b"unlisted")
        adapter = EncryptingAdapter(
            inner,
            kms,
            allow_plaintext_reads=True,
            legacy_inventory=LegacyInventory(objects={}),
        )
        assert adapter.get_object(KEY_NEW) == b"unlisted"
        assert adapter.inventory_reads == 0

    def test_chain_log_unaffected(
        self, legacy_store: InMemoryWormAdapter, kms: MockKmsBackend
    ) -> None:
        adapter = EncryptingAdapter(
            legacy_store, kms, legacy_inventory=LegacyInventory(objects={}),
            refuse_legacy_envelopes=True,
        )
        assert adapter.get_object(KEY_LOG) == b'{"version": 0}'


class TestStrictMode:
    def test_default_still_reads_v1(
        self, legacy_store: InMemoryWormAdapter, kms: MockKmsBackend
    ) -> None:
        adapter = EncryptingAdapter(legacy_store, kms)
        assert adapter.get_object(KEY_V1) == FIXTURE_PLAINTEXT
        assert adapter.legacy_envelope_reads == 1

    def test_strict_refuses_unlisted_v1(
        self, legacy_store: InMemoryWormAdapter, kms: MockKmsBackend
    ) -> None:
        adapter = EncryptingAdapter(legacy_store, kms, refuse_legacy_envelopes=True)
        with pytest.raises(LegacyEnvelopeRefusedError) as ei:
            adapter.get_object(KEY_V1)
        assert ei.value.key == KEY_V1
        assert isinstance(ei.value, EnvelopeEncryptionError)
        assert adapter.legacy_envelope_reads == 0
        assert adapter.legacy_refusals == 1

    def test_strict_admits_inventoried_v1(
        self, legacy_store: InMemoryWormAdapter, kms: MockKmsBackend
    ) -> None:
        adapter = EncryptingAdapter(
            legacy_store,
            kms,
            legacy_inventory=build_legacy_inventory(legacy_store),
            refuse_legacy_envelopes=True,
        )
        assert adapter.get_object(KEY_V1) == FIXTURE_PLAINTEXT
        assert adapter.inventory_reads == 1
        assert adapter.legacy_envelope_reads == 1

    def test_strict_refuses_v1_swapped_onto_new_key(
        self, legacy_store: InMemoryWormAdapter, kms: MockKmsBackend
    ) -> None:
        """The ADR-0290 residual gap: v1 envelopes are swappable among themselves."""
        adapter = EncryptingAdapter(
            legacy_store,
            kms,
            legacy_inventory=build_legacy_inventory(legacy_store),
            refuse_legacy_envelopes=True,
        )
        _store_raw(legacy_store, KEY_NEW, legacy_store.get_object(KEY_V1))
        with pytest.raises(LegacyEnvelopeRefusedError):
            adapter.get_object(KEY_NEW)

    def test_strict_leaves_v2_alone(
        self, legacy_store: InMemoryWormAdapter, kms: MockKmsBackend
    ) -> None:
        adapter = EncryptingAdapter(legacy_store, kms, refuse_legacy_envelopes=True)
        assert adapter.get_object(KEY_V2) == b"bound"
        assert adapter.legacy_refusals == 0


class TestLoadFailsClosed:
    def _write(self, tmp_path: Path, payload: object) -> Path:
        path = tmp_path / "inv.json"
        path.write_text(json.dumps(payload))
        return path

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(LegacyInventoryError, match="cannot read"):
            load_legacy_inventory(tmp_path / "absent.json")

    def test_not_json(self, tmp_path: Path) -> None:
        path = tmp_path / "inv.json"
        path.write_text("not json")
        with pytest.raises(LegacyInventoryError, match="not JSON"):
            load_legacy_inventory(path)

    @pytest.mark.parametrize(
        "payload",
        [
            [],
            {"objects": {}},
            {"format": "other", "objects": {}},
            {"format": INVENTORY_FORMAT, "objects": []},
            {"format": INVENTORY_FORMAT, "objects": {"k": "nothex"}},
            {"format": INVENTORY_FORMAT, "objects": {"": "0" * 64}},
            {"format": INVENTORY_FORMAT, "objects": {"k": "A" * 64}},
        ],
    )
    def test_malformed(self, tmp_path: Path, payload: object) -> None:
        with pytest.raises(LegacyInventoryError):
            load_legacy_inventory(self._write(tmp_path, payload))

    def test_pin_mismatch(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, {"format": INVENTORY_FORMAT, "objects": {}})
        with pytest.raises(LegacyInventoryError, match="pinned SHA-256"):
            load_legacy_inventory(path, expected_sha256="0" * 64)

    def test_pin_not_hex(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, {"format": INVENTORY_FORMAT, "objects": {}})
        with pytest.raises(LegacyInventoryError, match="not a SHA-256"):
            load_legacy_inventory(path, expected_sha256="xyz")


class TestEnvWiring:
    @pytest.fixture(autouse=True)
    def _encryption_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        kek = tmp_path / "kek.bin"
        kek.write_bytes(b"\x07" * 32)
        monkeypatch.setenv(ENV_ENCRYPTION, "1")
        monkeypatch.setenv(ENV_KEK_PATH, str(kek))
        for var in (
            ENV_ALLOW_PLAINTEXT_READS,
            ENV_LEGACY_INVENTORY,
            ENV_LEGACY_INVENTORY_SHA256,
            ENV_REFUSE_V1_ENVELOPES,
        ):
            monkeypatch.delenv(var, raising=False)

    def _inventory(self, tmp_path: Path, objects: dict[str, str]) -> Path:
        path = tmp_path / "inventory.json"
        path.write_text(LegacyInventory(objects=objects).to_json())
        return path

    def test_defaults_unchanged(self) -> None:
        adapter = make_adapter("local")
        assert isinstance(adapter, EncryptingAdapter)
        assert adapter._legacy_inventory is None  # noqa: SLF001
        assert adapter._refuse_legacy_envelopes is False  # noqa: SLF001

    def test_inventory_and_pin(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        path = self._inventory(
            tmp_path, {KEY_PLAIN: hashlib.sha256(b"raw").hexdigest()}
        )
        monkeypatch.setenv(ENV_LEGACY_INVENTORY, str(path))
        monkeypatch.setenv(
            ENV_LEGACY_INVENTORY_SHA256, hashlib.sha256(path.read_bytes()).hexdigest()
        )
        adapter = make_adapter("local")
        assert isinstance(adapter, EncryptingAdapter)
        _store_raw(adapter._inner, KEY_PLAIN, b"raw")  # noqa: SLF001
        _store_raw(adapter._inner, KEY_NEW, b"raw")  # noqa: SLF001
        assert adapter.get_object(KEY_PLAIN) == b"raw"
        with pytest.raises(PlaintextObjectRefusedError):
            adapter.get_object(KEY_NEW)

    def test_pin_mismatch_refuses_start(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(ENV_LEGACY_INVENTORY, str(self._inventory(tmp_path, {})))
        monkeypatch.setenv(ENV_LEGACY_INVENTORY_SHA256, "0" * 64)
        with pytest.raises(LegacyInventoryError):
            make_adapter("local")

    def test_pin_without_inventory_refuses_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_LEGACY_INVENTORY_SHA256, "0" * 64)
        with pytest.raises(ValueError, match="digest pin without an inventory"):
            make_adapter("local")

    def test_malformed_inventory_refuses_start(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{}")
        monkeypatch.setenv(ENV_LEGACY_INVENTORY, str(bad))
        with pytest.raises(ValueError):
            make_adapter("local")

    def test_strict_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_REFUSE_V1_ENVELOPES, "true")
        adapter = make_adapter("local")
        assert isinstance(adapter, EncryptingAdapter)
        _store_raw(adapter._inner, KEY_V1, FIXTURE.read_bytes())  # noqa: SLF001
        with pytest.raises(LegacyEnvelopeRefusedError):
            adapter.get_object(KEY_V1)

    def test_opt_in_with_inventory_warns(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        monkeypatch.setenv(ENV_LEGACY_INVENTORY, str(self._inventory(tmp_path, {})))
        monkeypatch.setenv(ENV_ALLOW_PLAINTEXT_READS, "1")
        with caplog.at_level(logging.WARNING):
            make_adapter("local")
        assert "overrides the legacy-object inventory" in caplog.text
