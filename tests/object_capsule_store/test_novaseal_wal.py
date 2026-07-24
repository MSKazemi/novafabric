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
"""Tests for NovaSeal WAL fallback and idempotent drain (adr-002, FR-06)."""

from __future__ import annotations

from novafabric.object_capsule_store.backend_router import InMemoryWormAdapter
from novafabric.object_capsule_store.client import ObjectCapsuleStore
from novafabric.object_capsule_store.local_wal import LocalWal
from novafabric.object_capsule_store.manifest_chain import ManifestChainWriter
from novafabric.object_capsule_store.models import ReceiptStatus
from novafabric.object_capsule_store.novaseal_client import (
    NovaSealClient,
    NovaSealDisabledClient,
)


def make_capsule(seed: int = 0, size: int = 256) -> bytes:
    import hashlib
    base = hashlib.sha256(seed.to_bytes(8, "big")).digest()
    return (base * (size // 32 + 1))[:size]


# ---------------------------------------------------------------------------
# WAL enqueue / drain
# ---------------------------------------------------------------------------

def test_wal_enqueue_and_pending_count():
    wal = LocalWal(":memory:")
    assert wal.pending_count() == 0
    row_id = wal.enqueue(
        capsule_uri="capsules/t/abcd/x/data.zst",
        capsule_sha256="a" * 64,
        tenant="t",
        run_id="r",
    )
    assert row_id is not None
    assert wal.pending_count() == 1
    wal.close()


def test_wal_mark_drained():
    wal = LocalWal(":memory:")
    row_id = wal.enqueue("uri", "a" * 64, "t", "r")
    assert wal.pending_count() == 1
    wal.mark_drained(row_id)
    assert wal.pending_count() == 0
    wal.close()


def test_wal_multiple_entries():
    wal = LocalWal(":memory:")
    for i in range(5):
        wal.enqueue(f"capsules/t/{i}/data.zst", "a" * 64, "t", f"r{i}")
    assert wal.pending_count() == 5
    entries = wal.pending_entries()
    assert len(entries) == 5
    wal.close()


# ---------------------------------------------------------------------------
# FR-06: NovaSeal unavailable → WAL fallback, PENDING_SIGNATURE
# ---------------------------------------------------------------------------

def test_put_capsule_novaseal_unavailable_falls_back_to_wal():
    """FR-06: NovaSeal raises → PENDING_SIGNATURE, chain commits == 0, WAL count == 1."""
    adapter = InMemoryWormAdapter()
    wal = LocalWal(":memory:")
    store = ObjectCapsuleStore(
        adapter=adapter,
        novaseal_client=NovaSealDisabledClient(),  # type: ignore[arg-type]
        wal=wal,
        retention_days=365,
    )
    content = make_capsule(seed=42)

    receipt = store.put_capsule(tenant="t", run_id="r", content_bytes=content)

    assert receipt.status == ReceiptStatus.PENDING_SIGNATURE
    assert receipt.wal_row_id is not None

    # Chain must have 0 commits
    chain_writer = ManifestChainWriter(adapter)
    chain = chain_writer.read_chain("t", "r")
    assert len(chain) == 0

    # WAL must have exactly 1 pending entry
    assert wal.pending_count() == 1
    wal.close()


# ---------------------------------------------------------------------------
# adr-002: WAL drain is idempotent
# ---------------------------------------------------------------------------

def test_drain_wal_idempotent(mock_novaseal_client: NovaSealClient):
    """drain_wal() is idempotent — draining twice is safe."""
    adapter = InMemoryWormAdapter()
    wal = LocalWal(":memory:")

    # First: put with disabled NovaSeal → goes to WAL
    store_no_seal = ObjectCapsuleStore(
        adapter=adapter,
        novaseal_client=NovaSealDisabledClient(),  # type: ignore[arg-type]
        wal=wal,
        retention_days=365,
    )
    content = make_capsule(seed=1)
    receipt = store_no_seal.put_capsule(tenant="t", run_id="r", content_bytes=content)
    assert receipt.status == ReceiptStatus.PENDING_SIGNATURE

    # Now swap in a working NovaSeal client and drain
    store_with_seal = ObjectCapsuleStore(
        adapter=adapter,
        novaseal_client=mock_novaseal_client,
        wal=wal,
        retention_days=365,
    )
    drained1 = store_with_seal.drain_wal()
    assert drained1 == 1
    assert wal.pending_count() == 0

    # Drain again — idempotent
    drained2 = store_with_seal.drain_wal()
    assert drained2 == 0
    wal.close()


def test_drain_wal_produces_chain_commit(mock_novaseal_client: NovaSealClient):
    """After successful drain, exactly one chain commit exists."""
    adapter = InMemoryWormAdapter()
    wal = LocalWal(":memory:")

    store_no_seal = ObjectCapsuleStore(
        adapter=adapter,
        novaseal_client=NovaSealDisabledClient(),  # type: ignore[arg-type]
        wal=wal,
        retention_days=365,
    )
    content = make_capsule(seed=99)
    store_no_seal.put_capsule(tenant="acme", run_id="run1", content_bytes=content)

    store_with_seal = ObjectCapsuleStore(
        adapter=adapter,
        novaseal_client=mock_novaseal_client,
        wal=wal,
        retention_days=365,
    )
    store_with_seal.drain_wal()

    chain_writer = ManifestChainWriter(adapter)
    chain = chain_writer.read_chain("acme", "run1")
    assert len(chain) == 1
    assert chain[0].novaseal_leaf_hash is not None
    wal.close()


class _PoisonNovaSealClient:
    """A NovaSeal client whose sign() always raises a NON-transient error."""

    def sign(self, capsule_sha256: str):  # noqa: ANN201
        raise ValueError("permanently broken signing input")


def test_wal_row_dead_lettered_after_max_attempts():
    """Enterprise-hardening 3.4: a row that keeps failing to drain for a
    non-transient reason is dead-lettered instead of retried forever."""
    adapter = InMemoryWormAdapter()
    wal = LocalWal(":memory:")
    content = make_capsule(seed=7, size=256)

    # Enqueue a pending entry (NovaSeal unavailable at upload time).
    store_no_seal = ObjectCapsuleStore(
        adapter=adapter,
        novaseal_client=NovaSealDisabledClient(),  # type: ignore[arg-type]
        wal=wal,
        retention_days=365,
    )
    store_no_seal.put_capsule(tenant="acme", run_id="poison", content_bytes=content)
    assert wal.pending_count() == 1

    # Drain with a client that fails non-transiently; cap attempts at 2.
    store_broken = ObjectCapsuleStore(
        adapter=adapter,
        novaseal_client=_PoisonNovaSealClient(),  # type: ignore[arg-type]
        wal=wal,
        retention_days=365,
        max_wal_attempts=2,
    )
    # Attempt 1: still pending (attempts=1 < 2).
    assert store_broken.drain_wal() == 0
    assert wal.pending_count() == 1
    assert wal.poisoned_count() == 0
    # Attempt 2: reaches the cap → dead-lettered, no longer pending.
    assert store_broken.drain_wal() == 0
    assert wal.pending_count() == 0
    assert wal.poisoned_count() == 1
    # Subsequent drains do not resurrect the poisoned row.
    assert store_broken.drain_wal() == 0
    assert wal.poisoned_count() == 1
    wal.close()


def test_no_capsule_loss_after_wal_drain(mock_novaseal_client: NovaSealClient):
    """AC-3: capsule bytes are retrievable after WAL drain — no capsule loss (BQ-013).

    Protocol:
    1. NovaSeal is unavailable → put_capsule returns PENDING_SIGNATURE.
       The capsule data IS uploaded (step 2 of the 5-step protocol); only the
       chain commit (step 5) is deferred.
    2. NovaSeal recovers → drain_wal() signs and appends the chain commit.
    3. get_capsule() retrieves the original bytes without error.
    """
    adapter = InMemoryWormAdapter()
    wal = LocalWal(":memory:")
    content = make_capsule(seed=42, size=512)

    # Phase 1: upload with NovaSeal unavailable
    store_no_seal = ObjectCapsuleStore(
        adapter=adapter,
        novaseal_client=NovaSealDisabledClient(),  # type: ignore[arg-type]
        wal=wal,
        retention_days=365,
    )
    receipt = store_no_seal.put_capsule(tenant="acme", run_id="run-loss", content_bytes=content)
    assert receipt.status == ReceiptStatus.PENDING_SIGNATURE
    capsule_uri = receipt.capsule_uri

    # Phase 2: NovaSeal recovers → drain
    store_with_seal = ObjectCapsuleStore(
        adapter=adapter,
        novaseal_client=mock_novaseal_client,
        wal=wal,
        retention_days=365,
    )
    drained = store_with_seal.drain_wal()
    assert drained == 1

    # Phase 3: capsule bytes must be retrievable (no capsule loss)
    retrieved = store_with_seal.get_capsule("acme", "run-loss", capsule_uri)
    assert retrieved == content
    wal.close()
