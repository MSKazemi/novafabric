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
"""Shared fixtures for Object Capsule Store tests."""

from __future__ import annotations

import pathlib
from typing import Generator
from unittest.mock import MagicMock

import pytest

from novafabric.object_capsule_store.backend_router import InMemoryWormAdapter
from novafabric.object_capsule_store.cas import compute_sha256
from novafabric.object_capsule_store.client import ObjectCapsuleStore
from novafabric.object_capsule_store.local_wal import LocalWal
from novafabric.object_capsule_store.novaseal_client import (
    NovaSealClient,
    NovaSealDisabledClient,
)

# ---------------------------------------------------------------------------
# In-memory adapter
# ---------------------------------------------------------------------------

@pytest.fixture
def mem_adapter() -> InMemoryWormAdapter:
    return InMemoryWormAdapter()


# ---------------------------------------------------------------------------
# Local WAL (temp file)
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_wal(tmp_path: "pathlib.Path") -> Generator[LocalWal, None, None]:  # type: ignore[name-defined]
    wal = LocalWal(tmp_path / "wal.db")
    yield wal
    wal.close()


@pytest.fixture
def mem_wal() -> Generator[LocalWal, None, None]:
    wal = LocalWal(":memory:")
    yield wal
    wal.close()


# ---------------------------------------------------------------------------
# NovaSeal mock client (always succeeds)
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_novaseal_client() -> NovaSealClient:
    """NovaSealClient that returns a deterministic fake leaf."""
    mock_seal = MagicMock()
    mock_seal.seal.side_effect = _fake_seal
    return NovaSealClient(novaseal=mock_seal)


def _fake_seal(manifest: dict) -> MagicMock:
    import json

    log_entry = {
        "capsule_id": manifest.get("capsule_sha256", ""),
        "leaf_index": 1,
    }
    _entry_bytes = json.dumps(log_entry, sort_keys=True, separators=(",", ":")).encode()
    bundle = MagicMock()
    bundle.dsse_envelope = b'{"payload":"dGVzdA","payloadType":"test","signatures":[]}'
    bundle.tsr = b""
    bundle.log_entry = log_entry
    bundle.capsule_id = manifest.get("capsule_sha256", "")
    return bundle


@pytest.fixture
def disabled_novaseal_client() -> NovaSealDisabledClient:
    return NovaSealDisabledClient()


# ---------------------------------------------------------------------------
# ObjectCapsuleStore (in-memory, mock NovaSeal)
# ---------------------------------------------------------------------------

@pytest.fixture
def store(
    mem_adapter: InMemoryWormAdapter,
    mock_novaseal_client: NovaSealClient,
    mem_wal: LocalWal,
) -> ObjectCapsuleStore:
    return ObjectCapsuleStore(
        adapter=mem_adapter,
        novaseal_client=mock_novaseal_client,
        wal=mem_wal,
        retention_days=365,
        checkpoint_every=10,
    )


@pytest.fixture
def store_no_seal(
    mem_adapter: InMemoryWormAdapter,
    disabled_novaseal_client: NovaSealDisabledClient,
    mem_wal: LocalWal,
) -> ObjectCapsuleStore:
    return ObjectCapsuleStore(
        adapter=mem_adapter,
        novaseal_client=disabled_novaseal_client,  # type: ignore[arg-type]
        wal=mem_wal,
        retention_days=365,
    )


# ---------------------------------------------------------------------------
# Synthetic capsule generator
# ---------------------------------------------------------------------------

def make_capsule(seed: int = 0, size: int = 256) -> bytes:
    """Return deterministic random-ish capsule bytes."""
    import hashlib

    base = hashlib.sha256(seed.to_bytes(8, "big")).digest()
    return (base * (size // 32 + 1))[:size]


def make_capsule_sha256(seed: int = 0, size: int = 256) -> tuple[bytes, str]:
    """Return (content_bytes, sha256_hex) pair."""
    content = make_capsule(seed, size)
    sha256 = compute_sha256(content)
    return content, sha256
