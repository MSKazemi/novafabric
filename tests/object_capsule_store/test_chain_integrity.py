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
"""Tests for hash-chain integrity verification in the manifest chain (OQ-027, BQ-013)."""

from __future__ import annotations

import hashlib

import pytest

from novafabric.object_capsule_store.backend_router import InMemoryWormAdapter
from novafabric.object_capsule_store.exceptions import ChainIntegrityError
from novafabric.object_capsule_store.manifest_chain import (
    ManifestChainWriter,
    _version_key,
)
from novafabric.object_capsule_store.models import ManifestCommit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chain(n: int = 3) -> tuple[InMemoryWormAdapter, ManifestChainWriter]:
    adapter = InMemoryWormAdapter()
    writer = ManifestChainWriter(adapter)
    for i in range(n):
        sha = f"{i:064x}"
        writer.append(
            tenant="t",
            run_id="r",
            capsule_uri=f"capsules/t/{sha[:4]}/{sha}/data.zst",
            capsule_sha256=sha,
        )
    return adapter, writer


# ---------------------------------------------------------------------------
# Happy-path: version contiguity
# ---------------------------------------------------------------------------

def test_read_chain_returns_versions_in_order() -> None:
    _, writer = _make_chain(5)
    commits = writer.read_chain("t", "r")
    assert [c.version for c in commits] == [1, 2, 3, 4, 5]


def test_new_commits_carry_prev_commit_hash() -> None:
    _, writer = _make_chain(3)
    commits = writer.read_chain("t", "r")
    assert commits[0].prev_commit_hash is None  # genesis
    assert commits[1].prev_commit_hash is not None  # hash of v1
    assert commits[2].prev_commit_hash is not None  # hash of v2


def test_prev_commit_hash_matches_expected() -> None:
    _, writer = _make_chain(3)
    commits = writer.read_chain("t", "r")
    # Verify v2.prev_commit_hash == sha256(v1.model_dump_json())
    expected = hashlib.sha256(commits[0].model_dump_json().encode("utf-8")).hexdigest()
    assert commits[1].prev_commit_hash == expected


# ---------------------------------------------------------------------------
# Error path: version gap
# ---------------------------------------------------------------------------

def test_version_gap_raises_chain_integrity_error() -> None:
    """Removing a commit from the middle of the chain is detected on read."""
    adapter, writer = _make_chain(4)
    # Delete the version-2 commit file from the store
    key_v2 = _version_key("t", "r", 2)
    del adapter._store[key_v2]  # type: ignore[attr-defined]

    with pytest.raises(ChainIntegrityError, match="Version contiguity failure"):
        writer.read_chain("t", "r")


def test_version_duplicate_detected() -> None:
    """If two commits have the same version, contiguity check raises."""
    adapter = InMemoryWormAdapter()
    writer = ManifestChainWriter(adapter)
    # Write v1 + v2 legitimately
    sha1, sha2 = f"{1:064x}", f"{2:064x}"
    writer.append("t", "r", f"capsules/t/0001/{sha1}/data.zst", sha1)
    writer.append("t", "r", f"capsules/t/0002/{sha2}/data.zst", sha2)

    # Inject a spurious duplicate at version 1 under a different key
    key_fake = _version_key("t", "r", 99)  # high version slot
    fake_commit = ManifestCommit(
        version=1,  # duplicate!
        timestamp_ms=999,
        operation="put",
        capsule_uri="capsules/t/fake/data.zst",
        capsule_sha256=sha1,
        run_id="r",
        tenant="t",
    )
    adapter.put_log_object(key_fake, fake_commit.model_dump_json().encode("utf-8"))

    with pytest.raises(ChainIntegrityError, match="Version contiguity failure"):
        writer.read_chain("t", "r")


# ---------------------------------------------------------------------------
# Error path: hash-chain break
# ---------------------------------------------------------------------------

def test_hash_chain_break_detected_on_tampered_commit() -> None:
    """A tampered commit body is detected when the next commit's prev_commit_hash is checked."""
    adapter, writer = _make_chain(3)

    # Overwrite v1 in the store with tampered bytes (same version, different payload)
    key_v1 = _version_key("t", "r", 1)
    original = ManifestCommit.model_validate_json(adapter.get_object(key_v1))
    tampered = original.model_copy(update={"capsule_sha256": "a" * 64})
    adapter._store[key_v1] = tampered.model_dump_json().encode("utf-8")  # type: ignore[attr-defined]

    # v2's prev_commit_hash was computed against the un-tampered v1 bytes,
    # so reading the chain now must fail.
    with pytest.raises(ChainIntegrityError, match="Hash-chain break"):
        writer.read_chain("t", "r")


# ---------------------------------------------------------------------------
# Backward compat: old commits without prev_commit_hash are accepted
# ---------------------------------------------------------------------------

def test_old_commits_without_prev_commit_hash_accepted() -> None:
    """Chains written before OQ-027 (no prev_commit_hash) pass integrity check."""
    adapter = InMemoryWormAdapter()
    writer = ManifestChainWriter(adapter)

    # Manually write old-style commits (no prev_commit_hash)
    for i in range(1, 4):
        sha = f"{i:064x}"
        commit = ManifestCommit(
            version=i,
            timestamp_ms=i * 1000,
            operation="put",
            capsule_uri=f"capsules/t/{sha[:4]}/{sha}/data.zst",
            capsule_sha256=sha,
            run_id="r",
            tenant="t",
            prev_commit_hash=None,  # no hash chain — old style
        )
        adapter.put_log_object(
            _version_key("t", "r", i),
            commit.model_dump_json().encode("utf-8"),
        )

    commits = writer.read_chain("t", "r")  # must not raise
    assert len(commits) == 3


# ---------------------------------------------------------------------------
# verify_integrity=False bypasses checks
# ---------------------------------------------------------------------------

def test_read_chain_verify_false_skips_integrity() -> None:
    """read_chain(verify_integrity=False) skips all checks (for fast bulk reads)."""
    adapter, writer = _make_chain(3)
    # Remove v2 — normally raises ChainIntegrityError
    key_v2 = _version_key("t", "r", 2)
    del adapter._store[key_v2]  # type: ignore[attr-defined]

    # With verify_integrity=False, no error is raised
    commits = writer.read_chain("t", "r", verify_integrity=False)
    assert len(commits) == 2  # v1, v3 returned without complaint
