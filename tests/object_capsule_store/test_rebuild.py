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
"""Tests for rebuild_metadata_db (FR-04)."""

from __future__ import annotations

from novafabric.object_capsule_store.backend_router import InMemoryWormAdapter
from novafabric.object_capsule_store.manifest_chain import ManifestChainWriter
from novafabric.object_capsule_store.rebuild import rebuild_metadata_db


def _populate_chain(
    adapter: InMemoryWormAdapter, tenant: str, n_runs: int, capsules_per_run: int
) -> int:
    """Populate chain log with n_runs × capsules_per_run commits. Returns total count."""
    writer = ManifestChainWriter(adapter)
    total = 0
    for i in range(n_runs):
        run_id = f"run-{i:04d}"
        for j in range(capsules_per_run):
            sha = f"{(i * capsules_per_run + j):064x}"
            uri = f"capsules/{tenant}/{sha[:4]}/{sha}/data.zst"
            writer.append(
                tenant=tenant,
                run_id=run_id,
                capsule_uri=uri,
                capsule_sha256=sha,
            )
            total += 1
    return total


def test_rebuild_empty():
    adapter = InMemoryWormAdapter()
    report = rebuild_metadata_db(adapter, prefix="", target_db=":memory:")
    assert report.capsules_found == 0
    assert report.time_to_replay_seconds >= 0


def test_rebuild_small():
    """FR-04: Rebuild from chain log produces correct capsule count."""
    adapter = InMemoryWormAdapter()
    total = _populate_chain(adapter, "acme", n_runs=3, capsules_per_run=5)

    report = rebuild_metadata_db(adapter, prefix="acme", target_db=":memory:")
    assert report.capsules_found == total
    assert len(report.integrity_warnings) == 0
    assert report.time_to_replay_seconds >= 0


def test_rebuild_100k_capsules():
    """FR-04: 100K-capsule rebuild (CI acceptance gate)."""
    adapter = InMemoryWormAdapter()
    # 100 runs × 1000 capsules = 100,000 total
    total = _populate_chain(adapter, "bigcorp", n_runs=100, capsules_per_run=1000)
    assert total == 100_000

    report = rebuild_metadata_db(adapter, prefix="bigcorp", target_db=":memory:")
    assert report.capsules_found == total
    assert len(report.integrity_warnings) == 0
    # Should complete in under 60 seconds
    assert report.time_to_replay_seconds < 60.0


def test_rebuild_prefix_isolation():
    """Rebuild with a specific prefix only rebuilds that tenant."""
    adapter = InMemoryWormAdapter()
    _populate_chain(adapter, "tenant_a", n_runs=2, capsules_per_run=3)
    _populate_chain(adapter, "tenant_b", n_runs=2, capsules_per_run=3)

    report = rebuild_metadata_db(adapter, prefix="tenant_a", target_db=":memory:")
    assert report.capsules_found == 6  # only tenant_a
