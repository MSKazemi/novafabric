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
"""Tests for Checkpoint Compactor (FR-03: byte-equivalent reconstruction)."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from novafabric.object_capsule_store.backend_router import InMemoryWormAdapter
from novafabric.object_capsule_store.checkpoint import CheckpointCompactor
from novafabric.object_capsule_store.manifest_chain import ManifestChainWriter


def _write_commits(adapter: InMemoryWormAdapter, tenant: str, run_id: str, n: int) -> None:
    writer = ManifestChainWriter(adapter)
    for i in range(n):
        writer.append(
            tenant=tenant,
            run_id=run_id,
            capsule_uri=f"capsules/{tenant}/abcd/{'a'*64}/data.zst",
            capsule_sha256="a" * 64,
            stats={"seq": i},
        )


# ---------------------------------------------------------------------------
# FR-03: Basic byte-equivalence
# ---------------------------------------------------------------------------

def test_checkpoint_then_replay_byte_equivalent():
    """FR-03: Checkpoint + delta replay == full replay from scratch."""
    adapter = InMemoryWormAdapter()
    tenant, run_id = "org", "run1"

    _write_commits(adapter, tenant, run_id, 5)

    compactor = CheckpointCompactor(adapter, checkpoint_every=5)
    result = compactor.force_checkpoint(tenant, run_id)
    assert result is not None
    assert result.commit_count == 5

    # Add more commits after checkpoint
    _write_commits(adapter, tenant, run_id, 3)

    # Replay via checkpoint + delta
    replayed = compactor.replay(tenant, run_id)

    # Full replay from scratch (re-read all individual files)
    writer = ManifestChainWriter(adapter)
    full = writer.read_chain(tenant, run_id)

    assert len(replayed) == len(full) == 8
    for r, f in zip(replayed, full):
        assert r.model_dump_json() == f.model_dump_json(), (
            f"Mismatch at version {r.version}: {r.model_dump_json()} != {f.model_dump_json()}"
        )


def test_checkpoint_file_written_at_threshold():
    """Checkpoint file is written when chain length reaches checkpoint_every."""
    adapter = InMemoryWormAdapter()
    compactor = CheckpointCompactor(adapter, checkpoint_every=3)
    writer = ManifestChainWriter(adapter)

    for i in range(3):
        writer.append(
            tenant="t", run_id="r",
            capsule_uri=f"capsules/t/abcd/{'a'*64}/data.zst",
            capsule_sha256="a" * 64,
        )
        result = compactor.maybe_checkpoint("t", "r")
        if i < 2:
            assert result is None
        else:
            assert result is not None
            assert result.checkpoint_version == 3


def test_replay_without_checkpoint():
    """replay() without any checkpoint falls back to full chain read."""
    adapter = InMemoryWormAdapter()
    compactor = CheckpointCompactor(adapter, checkpoint_every=100)
    writer = ManifestChainWriter(adapter)

    for _ in range(5):
        writer.append(
            tenant="t", run_id="r",
            capsule_uri=f"capsules/t/abcd/{'a'*64}/data.zst",
            capsule_sha256="a" * 64,
        )

    replayed = compactor.replay("t", "r")
    full = writer.read_chain("t", "r")
    assert [c.version for c in replayed] == [c.version for c in full]


# ---------------------------------------------------------------------------
# FR-03: Hypothesis property test — byte-equivalence on randomised sequences
# ---------------------------------------------------------------------------

@given(
    n_before=st.integers(min_value=1, max_value=30),
    n_after=st.integers(min_value=0, max_value=20),
)
@settings(max_examples=100)
def test_hypothesis_byte_equivalent_replay(n_before: int, n_after: int) -> None:
    """FR-03: ∀ (n_before, n_after): checkpoint @ n_before + delta replay == full replay."""
    adapter = InMemoryWormAdapter()
    compactor = CheckpointCompactor(adapter, checkpoint_every=n_before + n_after + 100)
    writer = ManifestChainWriter(adapter)

    _write_commits(adapter, "t", "r", n_before)
    compactor.force_checkpoint("t", "r")
    _write_commits(adapter, "t", "r", n_after)

    replayed = compactor.replay("t", "r")
    full = writer.read_chain("t", "r")

    assert len(replayed) == len(full)
    for r, f in zip(replayed, full):
        assert r.model_dump_json() == f.model_dump_json()
