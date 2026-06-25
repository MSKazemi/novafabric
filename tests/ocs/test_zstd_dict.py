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
"""Tests for ZstdDictRegistry and ObjectCapsuleStore compression integration (cap-005).

Test inventory (14 tests):
    1.  train_and_get_returns_bytes            — train/get round-trip
    2.  train_overwrites_existing              — dict_id update semantics
    3.  register_pre_trained_dict              — register() with raw bytes
    4.  get_unknown_returns_none               — get() returns None when absent
    5.  compress_decompress_round_trip         — basic compress/decompress identity
    6.  compress_raises_for_unknown_dict       — CompressionDictNotFound on compress
    7.  decompress_raises_for_unknown_dict     — CompressionDictNotFound on decompress
    8.  compression_reduces_size_for_repetitive_data — compressed < raw for repetitive input
    9.  thread_safety_concurrent_compress      — no data corruption under concurrent load
    10. put_capsule_with_compression_stores_compressed — chain SHA-256 matches compressed bytes
    11. get_capsule_decompresses_transparently — round-trip via store produces original bytes
    12. put_no_registry_with_dict_id_raises    — CompressionDictNotFound when no registry
    13. get_no_registry_raises_for_compressed  — CompressionDictNotFound on get when no registry
    14. put_without_compression_still_works    — registry=None, dict_id=None path is unaffected
"""

from __future__ import annotations

import concurrent.futures
import json
import pathlib
from typing import Generator
from unittest.mock import MagicMock

import pytest

from novafabric.object_capsule_store.backend_router import InMemoryWormAdapter
from novafabric.object_capsule_store.cas import compute_sha256
from novafabric.object_capsule_store.client import ObjectCapsuleStore
from novafabric.object_capsule_store.exceptions import CompressionDictNotFound
from novafabric.object_capsule_store.local_wal import LocalWal
from novafabric.object_capsule_store.novaseal_client import NovaSealClient
from novafabric.object_capsule_store.zstd_dict import ZstdDictRegistry

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_repetitive_capsule(size: int = 4096) -> bytes:
    """Return highly repetitive bytes that a zstd dict can compress well."""
    chunk = b'{"schema_version":"1.0","run_id":"run-abc","tenant":"acme",'
    chunk += b'"event_type":"llm_call","model":"gpt-4o","tokens":1024}'
    repeats = size // len(chunk) + 1
    return (chunk * repeats)[:size]


def _train_registry(num_samples: int = 20, size: int = 4096) -> ZstdDictRegistry:
    registry = ZstdDictRegistry()
    samples = [_make_repetitive_capsule(size) for _ in range(num_samples)]
    registry.train(samples=samples, dict_id="capsule-v1")
    return registry


def _fake_seal(manifest: dict) -> MagicMock:
    """Deterministic fake NovaSeal leaf."""
    log_entry = {"capsule_id": manifest.get("capsule_sha256", ""), "leaf_index": 1}
    bundle = MagicMock()
    bundle.dsse_envelope = b'{"payload":"dGVzdA","payloadType":"test","signatures":[]}'
    bundle.tsr = b""
    bundle.log_entry = log_entry
    bundle.capsule_id = manifest.get("capsule_sha256", "")
    return bundle


@pytest.fixture
def mock_novaseal_client() -> NovaSealClient:
    mock_seal = MagicMock()
    mock_seal.seal.side_effect = _fake_seal
    return NovaSealClient(novaseal=mock_seal)


@pytest.fixture
def mem_wal(tmp_path: pathlib.Path) -> Generator[LocalWal, None, None]:
    wal = LocalWal(":memory:")
    yield wal
    wal.close()


@pytest.fixture
def mem_adapter() -> InMemoryWormAdapter:
    return InMemoryWormAdapter()


def _make_store(
    adapter: InMemoryWormAdapter,
    novaseal_client: NovaSealClient,
    wal: LocalWal,
    zstd_registry: ZstdDictRegistry | None = None,
) -> ObjectCapsuleStore:
    return ObjectCapsuleStore(
        adapter=adapter,
        novaseal_client=novaseal_client,
        wal=wal,
        retention_days=365,
        checkpoint_every=100,
        zstd_registry=zstd_registry,
    )


# ---------------------------------------------------------------------------
# 1. ZstdDictRegistry.train() / get() round-trip
# ---------------------------------------------------------------------------

def test_train_and_get_returns_bytes() -> None:
    registry = ZstdDictRegistry()
    samples = [b"hello world" * 100 for _ in range(10)]
    raw = registry.train(samples=samples, dict_id="mydict")
    assert isinstance(raw, bytes)
    assert len(raw) > 0
    assert registry.get("mydict") == raw


# ---------------------------------------------------------------------------
# 2. train() overwrites existing dict silently
# ---------------------------------------------------------------------------

def test_train_overwrites_existing() -> None:
    registry = ZstdDictRegistry()
    samples_a = [b"aaa" * 200 for _ in range(10)]
    samples_b = [b"bbb" * 200 for _ in range(10)]
    raw_a = registry.train(samples=samples_a, dict_id="d1")
    raw_b = registry.train(samples=samples_b, dict_id="d1")
    assert registry.get("d1") == raw_b
    assert raw_a != raw_b  # distinct training data → distinct dicts


# ---------------------------------------------------------------------------
# 3. register() stores pre-trained bytes
# ---------------------------------------------------------------------------

def test_register_pre_trained_dict() -> None:
    registry = ZstdDictRegistry()
    fake_dict_bytes = b"\x37\xa4\x30\xec" + b"\x00" * 20  # minimal fake magic
    # register() should store whatever we hand it — no validation of dict format
    # (zstandard validates format at compress/decompress time)
    registry.register("pre-trained", fake_dict_bytes)
    assert registry.get("pre-trained") == fake_dict_bytes


# ---------------------------------------------------------------------------
# 4. get() returns None for unknown dict_id
# ---------------------------------------------------------------------------

def test_get_unknown_returns_none() -> None:
    registry = ZstdDictRegistry()
    assert registry.get("does-not-exist") is None


# ---------------------------------------------------------------------------
# 5. compress/decompress round-trip produces original bytes
# ---------------------------------------------------------------------------

def test_compress_decompress_round_trip() -> None:
    registry = _train_registry()
    original = _make_repetitive_capsule(2048)
    compressed = registry.compress(original, "capsule-v1")
    recovered = registry.decompress(compressed, "capsule-v1")
    assert recovered == original


# ---------------------------------------------------------------------------
# 6. compress() raises CompressionDictNotFound for unknown dict_id
# ---------------------------------------------------------------------------

def test_compress_raises_for_unknown_dict() -> None:
    registry = ZstdDictRegistry()
    with pytest.raises(CompressionDictNotFound) as exc_info:
        registry.compress(b"data", "ghost-dict")
    assert "ghost-dict" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 7. decompress() raises CompressionDictNotFound for unknown dict_id
# ---------------------------------------------------------------------------

def test_decompress_raises_for_unknown_dict() -> None:
    registry = ZstdDictRegistry()
    with pytest.raises(CompressionDictNotFound) as exc_info:
        registry.decompress(b"\x28\xb5\x2f\xfd\x00", "missing")
    assert "missing" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 8. Compression produces smaller output for repetitive data
# ---------------------------------------------------------------------------

def test_compression_reduces_size_for_repetitive_data() -> None:
    registry = _train_registry(num_samples=30, size=8192)
    data = _make_repetitive_capsule(8192)
    compressed = registry.compress(data, "capsule-v1")
    assert len(compressed) < len(data), (
        f"Expected compressed ({len(compressed)}) < raw ({len(data)})"
    )


# ---------------------------------------------------------------------------
# 9. Thread-safety: concurrent compress calls do not corrupt output
# ---------------------------------------------------------------------------

def test_thread_safety_concurrent_compress() -> None:
    registry = _train_registry(num_samples=20, size=2048)
    originals = [_make_repetitive_capsule(512 + i * 7) for i in range(40)]

    def compress_and_verify(payload: bytes) -> bool:
        compressed = registry.compress(payload, "capsule-v1")
        recovered = registry.decompress(compressed, "capsule-v1")
        return recovered == payload

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(compress_and_verify, originals))

    assert all(results), "Some threads produced corrupt decompression output"


# ---------------------------------------------------------------------------
# 10. put_capsule() with compression_dict_id: chain SHA-256 is of compressed bytes
# ---------------------------------------------------------------------------

def test_put_capsule_with_compression_stores_compressed(
    mem_adapter: InMemoryWormAdapter,
    mock_novaseal_client: NovaSealClient,
    mem_wal: LocalWal,
) -> None:
    registry = _train_registry()
    store = _make_store(mem_adapter, mock_novaseal_client, mem_wal, registry)

    original = _make_repetitive_capsule(4096)
    receipt = store.put_capsule(
        tenant="acme", run_id="r1", content_bytes=original, compression_dict_id="capsule-v1"
    )
    assert receipt.version == 1

    # The SHA-256 stored in the chain should be of the *compressed* bytes,
    # not the original uncompressed bytes.
    compressed = registry.compress(original, "capsule-v1")
    expected_sha256 = compute_sha256(compressed)
    assert receipt.capsule_sha256 == expected_sha256


# ---------------------------------------------------------------------------
# 11. get_capsule() decompresses transparently — round-trip identity
# ---------------------------------------------------------------------------

def test_get_capsule_decompresses_transparently(
    mem_adapter: InMemoryWormAdapter,
    mock_novaseal_client: NovaSealClient,
    mem_wal: LocalWal,
) -> None:
    registry = _train_registry()
    store = _make_store(mem_adapter, mock_novaseal_client, mem_wal, registry)

    original = _make_repetitive_capsule(4096)
    receipt = store.put_capsule(
        tenant="acme", run_id="r2", content_bytes=original, compression_dict_id="capsule-v1"
    )

    recovered = store.get_capsule(tenant="acme", run_id="r2", capsule_uri=receipt.capsule_uri)
    assert recovered == original


# ---------------------------------------------------------------------------
# 12. put_capsule() with compression_dict_id but no registry raises
# ---------------------------------------------------------------------------

def test_put_no_registry_with_dict_id_raises(
    mem_adapter: InMemoryWormAdapter,
    mock_novaseal_client: NovaSealClient,
    mem_wal: LocalWal,
) -> None:
    store = _make_store(mem_adapter, mock_novaseal_client, mem_wal, zstd_registry=None)
    with pytest.raises(CompressionDictNotFound) as exc_info:
        store.put_capsule(
            tenant="acme",
            run_id="r3",
            content_bytes=b"payload",
            compression_dict_id="capsule-v1",
        )
    assert "capsule-v1" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 13. get_capsule() with no registry raises when capsule was stored compressed
# ---------------------------------------------------------------------------

def test_get_no_registry_raises_for_compressed(
    mock_novaseal_client: NovaSealClient,
) -> None:
    # Store side: use a registry so we can put_capsule with compression.
    mem_adapter_write = InMemoryWormAdapter()
    wal_write = LocalWal(":memory:")
    registry = _train_registry()
    store_write = _make_store(mem_adapter_write, mock_novaseal_client, wal_write, registry)

    original = _make_repetitive_capsule(2048)
    receipt = store_write.put_capsule(
        tenant="t1", run_id="rX", content_bytes=original, compression_dict_id="capsule-v1"
    )
    wal_write.close()

    # Read side: re-use same adapter but no registry.
    wal_read = LocalWal(":memory:")
    store_read = _make_store(mem_adapter_write, mock_novaseal_client, wal_read, zstd_registry=None)
    with pytest.raises(CompressionDictNotFound) as exc_info:
        store_read.get_capsule(tenant="t1", run_id="rX", capsule_uri=receipt.capsule_uri)
    assert "capsule-v1" in str(exc_info.value)
    wal_read.close()


# ---------------------------------------------------------------------------
# 14. put_capsule() without compression_dict_id still works (no regression)
# ---------------------------------------------------------------------------

def test_put_without_compression_still_works(
    mem_adapter: InMemoryWormAdapter,
    mock_novaseal_client: NovaSealClient,
    mem_wal: LocalWal,
) -> None:
    # No registry at all — must be fully opt-in.
    store = _make_store(mem_adapter, mock_novaseal_client, mem_wal, zstd_registry=None)
    payload = json.dumps({"run_id": "run-001", "tokens": 512}).encode()
    receipt = store.put_capsule(tenant="default", run_id="r-nocomp", content_bytes=payload)
    assert receipt.version == 1

    recovered = store.get_capsule(
        tenant="default", run_id="r-nocomp", capsule_uri=receipt.capsule_uri
    )
    assert recovered == payload
