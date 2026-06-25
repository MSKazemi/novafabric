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
"""Tests for reconcile_orphans (FR-09)."""

from __future__ import annotations

from novafabric.object_capsule_store.backend_router import InMemoryWormAdapter
from novafabric.object_capsule_store.cas import derive_key
from novafabric.object_capsule_store.manifest_chain import ManifestChainWriter
from novafabric.object_capsule_store.rebuild import reconcile_orphans


def test_no_orphans():
    """No orphans when every data object has a chain commit."""
    adapter = InMemoryWormAdapter()
    tenant, run_id = "t", "r"
    sha = "a" * 64
    key = derive_key(tenant, sha)

    # Upload data
    content = b"capsule content"
    adapter.put_log_object(key, content)

    # Write chain commit
    writer = ManifestChainWriter(adapter)
    writer.append(
        tenant=tenant, run_id=run_id,
        capsule_uri=key, capsule_sha256=sha,
    )

    report = reconcile_orphans(adapter, tenant, dry_run=True)
    assert report.scanned == 1
    assert report.orphans_found == 0


def test_orphan_detected_dry_run():
    """Orphan detected when data object has no chain commit (dry_run=True)."""
    adapter = InMemoryWormAdapter()
    tenant = "org"
    sha = "b" * 64
    key = derive_key(tenant, sha)

    # Upload data but NO chain commit
    adapter.put_log_object(key, b"orphan capsule")

    report = reconcile_orphans(adapter, tenant, dry_run=True)
    assert report.scanned >= 1
    assert report.orphans_found >= 1
    assert report.tombstones_written == 0  # dry_run


def test_orphan_tombstone_written_not_dry_run():
    """Orphan gets tombstone when dry_run=False."""
    adapter = InMemoryWormAdapter()
    tenant = "org"
    sha = "c" * 64
    key = derive_key(tenant, sha)

    adapter.put_log_object(key, b"orphan capsule")

    report = reconcile_orphans(adapter, tenant, dry_run=False)
    assert report.orphans_found >= 1
    assert report.tombstones_written >= 1


def test_no_hard_delete():
    """reconcile_orphans NEVER deletes objects (FR-09: no hard delete)."""
    adapter = InMemoryWormAdapter()
    tenant = "org"
    sha = "d" * 64
    key = derive_key(tenant, sha)
    adapter.put_log_object(key, b"orphan capsule")

    reconcile_orphans(adapter, tenant, dry_run=False)

    # Object must still exist
    assert adapter.object_exists(key)
