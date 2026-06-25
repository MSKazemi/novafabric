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
"""Capsule Manifest Chain Writer (cap-001 — FR-01, FR-02, FR-05).

Writes one immutable JSON commit per ``put_capsule()`` call to:

    _capsule_log/<tenant>/<run_id>/<version>.json

Key design invariants:
- Per-(tenant, run_id) sharding — no global singleton (FR-05).
- Conditional-PUT via ``put_log_object_if_absent()`` (If-None-Match: * semantics)
  for the first commit; version-keyed files are already unique per version
  so subsequent commits write to a new key and never collide (FR-02).
- Only step 5 of the 5-step protocol makes a capsule visible to consumers
  (FR-06): writing the chain commit is the "commit point".
- Chain log objects are NOT WORM-locked (per build-prompt hard constraint).
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from novafabric.object_capsule_store.exceptions import ChainCommitConflict, ChainIntegrityError
from novafabric.object_capsule_store.models import ManifestCommit
from novafabric.object_capsule_store.worm.base import ConditionalPutConflict, WormAdapter

log = logging.getLogger(__name__)

# Maximum retries on conditional-PUT conflict (version collision is impossible
# because version numbers are globally unique per (tenant, run_id) — but we
# include retry logic for robustness in pathological edge cases).
_MAX_RETRIES = 32

# ---------------------------------------------------------------------------
# Chain key helpers
# ---------------------------------------------------------------------------

def _version_key(tenant: str, run_id: str, version: int) -> str:
    """Return the object-store key for a single chain commit."""
    return f"_capsule_log/{tenant}/{run_id}/{version:010d}.json"


def _tombstone_key(tenant: str, run_id: str, version: int) -> str:
    """Return the key for a tombstone commit."""
    return f"_capsule_log/{tenant}/{run_id}/{version:010d}.json"


def _chain_prefix(tenant: str, run_id: str) -> str:
    return f"_capsule_log/{tenant}/{run_id}/"


# ---------------------------------------------------------------------------
# Chain state — current head version
# ---------------------------------------------------------------------------

def _get_current_version(adapter: WormAdapter, tenant: str, run_id: str) -> int:
    """Scan chain log prefix and return the highest committed version number.

    Returns 0 if no commits exist yet.
    """
    prefix = _chain_prefix(tenant, run_id)
    keys = adapter.list_objects(prefix)
    versions = []
    for k in keys:
        fname = k.split("/")[-1]
        # strip .json or .checkpoint.ndjson suffix
        stem = fname.replace(".checkpoint.ndjson", "").replace(".json", "")
        try:
            versions.append(int(stem))
        except ValueError:
            pass
    return max(versions) if versions else 0


# ---------------------------------------------------------------------------
# Chain Writer
# ---------------------------------------------------------------------------

class ManifestChainWriter:
    """Appends immutable JSON commit objects to the per-(tenant, run_id) chain.

    Args:
        adapter:           WormAdapter instance (chain objects are written via
                           ``put_log_object`` / ``put_log_object_if_absent``).
        max_retries:       Maximum retries on conflict before raising
                           ``ChainCommitConflict``.
    """

    def __init__(self, adapter: WormAdapter, max_retries: int = _MAX_RETRIES) -> None:
        self._adapter = adapter
        self._max_retries = max_retries
        # Per-(tenant, run_id) version cache — avoids O(n) list_objects on every append.
        # This is an optimisation for sequential single-writer usage.
        # Concurrent writers must still handle ConditionalPutConflict.
        self._version_cache: dict[tuple[str, str], int] = {}

    def _get_next_version(self, tenant: str, run_id: str) -> int:
        """Return the next version for (tenant, run_id), using cache when possible."""
        key = (tenant, run_id)
        if key in self._version_cache:
            return self._version_cache[key] + 1
        # Cache miss — scan the store once
        current = _get_current_version(self._adapter, tenant, run_id)
        return current + 1

    def _commit_version(self, tenant: str, run_id: str, version: int) -> None:
        """Record that *version* was successfully committed."""
        self._version_cache[(tenant, run_id)] = version

    def append(
        self,
        tenant: str,
        run_id: str,
        capsule_uri: str,
        capsule_sha256: str,
        novaseal_leaf_hash: str | None = None,
        signed_at: str | None = None,
        operation: str = "put",
        stats: dict[str, Any] | None = None,
        compression_dict_id: str | None = None,
    ) -> ManifestCommit:
        """Append one commit to the chain and return the written ``ManifestCommit``.

        Uses optimistic concurrency:
        - Uses an in-process version cache for single-writer performance (O(1) per write).
        - On ConditionalPutConflict (concurrent writer), re-reads the store and retries.
        - Writes to ``<version+1>.json`` using ``put_log_object_if_absent``
          (If-None-Match: * semantics) for the very first commit.
        - For subsequent versions (version > 0), writes to a *new* key
          ``<next_version>.json`` — these never conflict because each version
          number is unique per (tenant, run_id).

        Each commit includes ``prev_commit_hash`` (OQ-027 hash-chain linkage):
        - Version 1: prev_commit_hash=None (genesis).
        - Version N>1: SHA-256 of the immediately preceding commit's canonical JSON.

        Raises:
            ChainCommitConflict: if retries are exhausted.
        """
        for attempt in range(self._max_retries):
            next_version = self._get_next_version(tenant, run_id)

            # Compute prev_commit_hash for hash-chain linkage (OQ-027).
            # Hash is computed over model_dump_json() of the *parsed* previous commit
            # (not raw stored bytes) so that the verification path in _verify_chain_integrity
            # can reproduce the same value without raw-byte access.
            prev_commit_hash: str | None = None
            if next_version > 1:
                prev_key = _version_key(tenant, run_id, next_version - 1)
                try:
                    prev_raw = self._adapter.get_object(prev_key)
                    prev_commit_obj = ManifestCommit.model_validate_json(prev_raw)
                    prev_commit_hash = hashlib.sha256(
                        prev_commit_obj.model_dump_json().encode("utf-8")
                    ).hexdigest()
                except Exception:
                    # Previous commit not readable — skip hash-chain (write still proceeds)
                    pass

            commit = ManifestCommit(
                version=next_version,
                timestamp_ms=int(time.time() * 1000),
                operation=operation,
                capsule_uri=capsule_uri,
                capsule_sha256=capsule_sha256,
                run_id=run_id,
                tenant=tenant,
                novaseal_leaf_hash=novaseal_leaf_hash,
                signed_at=signed_at,
                stats=stats or {},
                compression_dict_id=compression_dict_id,
                prev_commit_hash=prev_commit_hash,
            )
            data = commit.model_dump_json().encode("utf-8")
            key = _version_key(tenant, run_id, next_version)
            try:
                if next_version == 1:
                    # First-ever commit — use conditional-PUT (If-None-Match: *)
                    self._adapter.put_log_object_if_absent(key, data)
                else:
                    # Each version key is unique — plain put is safe and avoids
                    # needless If-None-Match round-trips.
                    self._adapter.put_log_object(key, data)
                self._commit_version(tenant, run_id, next_version)
                log.debug(
                    "Chain commit written: tenant=%s run_id=%s version=%d",
                    tenant, run_id, next_version,
                )
                return commit
            except ConditionalPutConflict:
                # Invalidate cache — another writer committed concurrently
                self._version_cache.pop((tenant, run_id), None)
                log.debug(
                    "Chain commit conflict (attempt %d/%d): tenant=%s run_id=%s next_version=%d",
                    attempt + 1, self._max_retries, tenant, run_id, next_version,
                )
                continue
        raise ChainCommitConflict(tenant=tenant, run_id=run_id, retries=self._max_retries)

    def read_chain(
        self, tenant: str, run_id: str, verify_integrity: bool = True
    ) -> list[ManifestCommit]:
        """Read and return all commits for (tenant, run_id) in version order.

        Args:
            verify_integrity: If True (default), enforce OQ-027 integrity checks:
                - Version contiguity: versions must be 1, 2, ..., N with no gaps
                  or duplicates.
                - Hash-chain linkage: when ``prev_commit_hash`` is present in a
                  commit, verify it equals SHA-256 of the previous commit's
                  canonical JSON.  Old commits without the field are skipped.

        Raises:
            ChainIntegrityError: if any integrity check fails and
                ``verify_integrity=True``.
        """
        prefix = _chain_prefix(tenant, run_id)
        keys = self._adapter.list_objects(prefix)
        commits: list[ManifestCommit] = []
        for key in sorted(keys):
            if key.endswith(".checkpoint.ndjson"):
                continue  # handled by checkpoint.py
            raw = self._adapter.get_object(key)
            commits.append(ManifestCommit.model_validate_json(raw))
        commits = sorted(commits, key=lambda c: c.version)

        if verify_integrity and commits:
            _verify_chain_integrity(tenant, run_id, commits)

        return commits

    def read_since(self, tenant: str, run_id: str, after_version: int) -> list[ManifestCommit]:
        """Return all commits with version > *after_version*."""
        return [c for c in self.read_chain(tenant, run_id) if c.version > after_version]

    def append_tombstone(
        self,
        tenant: str,
        run_id: str,
        capsule_uri: str,
        capsule_sha256: str,
    ) -> ManifestCommit:
        """Append a tombstone (logical deletion marker) commit (FR-09)."""
        return self.append(
            tenant=tenant,
            run_id=run_id,
            capsule_uri=capsule_uri,
            capsule_sha256=capsule_sha256,
            operation="tombstone",
        )


# ---------------------------------------------------------------------------
# Integrity helpers (OQ-027)
# ---------------------------------------------------------------------------

def _verify_chain_integrity(
    tenant: str, run_id: str, commits: list[ManifestCommit]
) -> None:
    """Verify version contiguity and hash-chain linkage for a sorted commit list.

    Args:
        tenant:  Tenant identifier (for error messages).
        run_id:  Run identifier (for error messages).
        commits: Sorted list of ``ManifestCommit`` objects (version ascending).

    Raises:
        ChainIntegrityError: on version gap, duplicate, or hash-chain break.
    """
    prev_bytes: bytes | None = None
    for i, commit in enumerate(commits):
        expected_version = i + 1
        if commit.version != expected_version:
            raise ChainIntegrityError(
                tenant=tenant,
                run_id=run_id,
                reason=(
                    f"Version contiguity failure at position {i}: "
                    f"expected version {expected_version}, got {commit.version}"
                ),
            )
        # Hash-chain check — only when prev_commit_hash is present (new commits)
        if commit.prev_commit_hash is not None and prev_bytes is not None:
            expected_hash = hashlib.sha256(prev_bytes).hexdigest()
            if commit.prev_commit_hash != expected_hash:
                raise ChainIntegrityError(
                    tenant=tenant,
                    run_id=run_id,
                    reason=(
                        f"Hash-chain break at version {commit.version}: "
                        f"expected prev_commit_hash={expected_hash!r}, "
                        f"got {commit.prev_commit_hash!r}"
                    ),
                )
        prev_bytes = commit.model_dump_json().encode("utf-8")
