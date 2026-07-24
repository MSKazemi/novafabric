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
"""Tests for Manifest Chain Writer (FR-01, FR-02, FR-05)."""

from __future__ import annotations

import concurrent.futures
import threading

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from novafabric.object_capsule_store.backend_router import InMemoryWormAdapter
from novafabric.object_capsule_store.manifest_chain import ManifestChainWriter

# ---------------------------------------------------------------------------
# FR-01: N put_capsule() → exactly N commits, monotonically increasing version
# ---------------------------------------------------------------------------

def test_chain_append_monotonic_versions():
    """FR-01: N appends → N commits with monotonically increasing version."""
    adapter = InMemoryWormAdapter()
    writer = ManifestChainWriter(adapter)
    n = 50
    for i in range(n):
        commit = writer.append(
            tenant="t1",
            run_id="r1",
            capsule_uri=f"capsules/t1/abcd/{'a'*64}/data.zst",
            capsule_sha256="a" * 64,
        )
        assert commit.version == i + 1

    chain = writer.read_chain("t1", "r1")
    assert len(chain) == n
    versions = [c.version for c in chain]
    assert versions == list(range(1, n + 1))


def test_chain_commit_schema_valid():
    """FR-01: Each commit must be schema-valid."""
    import json
    from pathlib import Path

    import jsonschema

    schema_path = (
        Path(__file__).parent.parent.parent
        / "src/novafabric/object_capsule_store/schemas/manifest_commit_v1.schema.json"
    )
    schema = json.loads(schema_path.read_text())
    validator = jsonschema.Draft202012Validator(schema)

    adapter = InMemoryWormAdapter()
    writer = ManifestChainWriter(adapter)
    commit = writer.append(
        tenant="acme",
        run_id="run-001",
        capsule_uri=f"capsules/acme/abcd/{'a'*64}/data.zst",
        capsule_sha256="a" * 64,
    )
    # Validate the commit dict
    instance = json.loads(commit.model_dump_json())
    errors = list(validator.iter_errors(instance))
    assert errors == [], f"Schema errors: {errors}"


def test_chain_byte_identical_on_reread():
    """FR-01: Commits are byte-identical on re-read."""
    adapter = InMemoryWormAdapter()
    writer = ManifestChainWriter(adapter)

    commit = writer.append(
        tenant="org",
        run_id="run-99",
        capsule_uri=f"capsules/org/1234/{'1'*64}/data.zst",
        capsule_sha256="1" * 64,
    )
    original_json = commit.model_dump_json()

    # Re-read from adapter
    chain = writer.read_chain("org", "run-99")
    assert len(chain) == 1
    assert chain[0].model_dump_json() == original_json


# ---------------------------------------------------------------------------
# FR-05: Per-run_id sharding — no global singleton
# ---------------------------------------------------------------------------

def test_per_run_id_sharding():
    """FR-05: Different run_ids have independent chains."""
    adapter = InMemoryWormAdapter()
    writer = ManifestChainWriter(adapter)

    for run_id in ["r1", "r2", "r3"]:
        for _ in range(5):
            writer.append(
                tenant="t1",
                run_id=run_id,
                capsule_uri=f"capsules/t1/abcd/{'a'*64}/data.zst",
                capsule_sha256="a" * 64,
            )

    for run_id in ["r1", "r2", "r3"]:
        chain = writer.read_chain("t1", run_id)
        assert len(chain) == 5
        # Each chain starts at version 1
        assert chain[0].version == 1

    # Verify no global path collision
    all_keys = adapter.all_keys()
    for run_id in ["r1", "r2", "r3"]:
        run_keys = [k for k in all_keys if f"/{run_id}/" in k]
        assert len(run_keys) == 5, f"Expected 5 keys for run_id={run_id}, got {len(run_keys)}"


def test_no_global_singleton_key():
    """FR-05: No chain key resolves to a single global path."""
    adapter = InMemoryWormAdapter()
    writer = ManifestChainWriter(adapter)

    # Write to multiple tenants and run_ids
    for tenant in ["org_a", "org_b"]:
        for run_id in ["run_x", "run_y"]:
            writer.append(
                tenant=tenant,
                run_id=run_id,
                capsule_uri=f"capsules/{tenant}/abcd/{'a'*64}/data.zst",
                capsule_sha256="a" * 64,
            )

    all_keys = adapter.all_keys()
    # All keys must be partitioned under _capsule_log/<tenant>/<run_id>/
    for key in all_keys:
        parts = key.split("/")
        assert len(parts) >= 4
        assert parts[0] == "_capsule_log"


# ---------------------------------------------------------------------------
# FR-02: Concurrent writers (thread-safety of InMemoryWormAdapter)
# ---------------------------------------------------------------------------

def test_concurrent_writers_no_duplicate_versions():
    """FR-02: Concurrent threads writing to the same (tenant, run_id) — no duplicates."""
    adapter = InMemoryWormAdapter()
    # Use a lock to make InMemory thread-safe (real backends use conditional-PUT)
    lock = threading.Lock()

    original_put_log = adapter.put_log_object
    original_put_if_absent = adapter.put_log_object_if_absent
    original_list = adapter.list_objects

    def locked_put(key: str, data: bytes) -> str:
        with lock:
            return original_put_log(key, data)

    def locked_put_if_absent(key: str, data: bytes) -> str:
        with lock:
            return original_put_if_absent(key, data)

    def locked_list(prefix: str) -> list[str]:
        with lock:
            return original_list(prefix)

    adapter.put_log_object = locked_put  # type: ignore[method-assign]
    adapter.put_log_object_if_absent = locked_put_if_absent  # type: ignore[method-assign]
    adapter.list_objects = locked_list  # type: ignore[method-assign]

    writer = ManifestChainWriter(adapter)

    n_threads = 8
    commits_per_thread = 10
    total = n_threads * commits_per_thread
    versions_seen: list[int] = []
    versions_lock = threading.Lock()

    def worker() -> None:
        for _ in range(commits_per_thread):
            c = writer.append(
                tenant="t",
                run_id="r",
                capsule_uri=f"capsules/t/abcd/{'a'*64}/data.zst",
                capsule_sha256="a" * 64,
            )
            with versions_lock:
                versions_seen.append(c.version)

    with concurrent.futures.ThreadPoolExecutor(max_workers=n_threads) as pool:
        futs = [pool.submit(worker) for _ in range(n_threads)]
        for f in futs:
            f.result()

    assert len(versions_seen) == total
    assert len(set(versions_seen)) == total, "Duplicate versions detected"
    assert set(versions_seen) == set(range(1, total + 1))


def test_append_fails_closed_when_predecessor_unreadable():
    """Enterprise-hardening 3.3: a version > 1 whose predecessor cannot be read
    must raise ChainIntegrityError rather than silently writing
    prev_commit_hash=None (which would forge a genesis-looking commit)."""
    from novafabric.object_capsule_store.exceptions import ChainIntegrityError
    from novafabric.object_capsule_store.manifest_chain import _version_key

    adapter = InMemoryWormAdapter()
    writer = ManifestChainWriter(adapter)
    writer.append(
        tenant="t", run_id="r",
        capsule_uri=f"capsules/t/abcd/{'a'*64}/data.zst", capsule_sha256="a" * 64,
    )
    # Remove v1 from the store but keep the writer's version cache, so the next
    # append computes next_version=2 and then cannot read its predecessor.
    adapter.delete_object(_version_key("t", "r", 1))

    with pytest.raises(ChainIntegrityError):
        writer.append(
            tenant="t", run_id="r",
            capsule_uri=f"capsules/t/abcd/{'b'*64}/data.zst", capsule_sha256="b" * 64,
        )


def test_stale_writer_does_not_overwrite_existing_version():
    """Enterprise-hardening 3.2: a second writer with a stale (empty) version
    cache must not overwrite an existing version — conditional-PUT forces it to
    retry onto a fresh version, preserving every commit."""
    adapter = InMemoryWormAdapter()
    writer_a = ManifestChainWriter(adapter)
    writer_a.append(
        tenant="t", run_id="r",
        capsule_uri=f"capsules/t/abcd/{'a'*64}/data.zst", capsule_sha256="a" * 64,
    )
    writer_a.append(
        tenant="t", run_id="r",
        capsule_uri=f"capsules/t/abcd/{'b'*64}/data.zst", capsule_sha256="b" * 64,
    )
    # Fresh writer over the same store — its cache is empty, so it initially
    # believes the next version is 1 (already taken). It must land on v3.
    writer_b = ManifestChainWriter(adapter)
    commit = writer_b.append(
        tenant="t", run_id="r",
        capsule_uri=f"capsules/t/abcd/{'c'*64}/data.zst", capsule_sha256="c" * 64,
    )
    chain = writer_b.read_chain("t", "r")
    assert commit.version == 3
    assert [c.version for c in chain] == [1, 2, 3]
    # v1 and v2 payloads survived (no overwrite).
    assert chain[0].capsule_sha256 == "a" * 64
    assert chain[1].capsule_sha256 == "b" * 64


# ---------------------------------------------------------------------------
# Hypothesis: chain versions always form a dense sequence
# ---------------------------------------------------------------------------

@given(n=st.integers(min_value=1, max_value=50))
@settings(max_examples=50)
def test_chain_versions_dense_hypothesis(n: int) -> None:
    """Property: N appends always produce dense version set {1..N}."""
    adapter = InMemoryWormAdapter()
    writer = ManifestChainWriter(adapter)
    for _ in range(n):
        writer.append(
            tenant="t",
            run_id="r",
            capsule_uri=f"capsules/t/abcd/{'a'*64}/data.zst",
            capsule_sha256="a" * 64,
        )
    chain = writer.read_chain("t", "r")
    assert [c.version for c in chain] == list(range(1, n + 1))


# ---------------------------------------------------------------------------
# Tombstone
# ---------------------------------------------------------------------------

def test_tombstone_appended():
    adapter = InMemoryWormAdapter()
    writer = ManifestChainWriter(adapter)
    writer.append(
        tenant="t",
        run_id="r",
        capsule_uri=f"capsules/t/abcd/{'a'*64}/data.zst",
        capsule_sha256="a" * 64,
    )
    tombstone = writer.append_tombstone(
        tenant="t",
        run_id="r",
        capsule_uri=f"capsules/t/abcd/{'a'*64}/data.zst",
        capsule_sha256="a" * 64,
    )
    assert tombstone.operation == "tombstone"
    assert tombstone.version == 2
