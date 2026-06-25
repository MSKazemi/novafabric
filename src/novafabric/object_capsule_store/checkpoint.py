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
"""Periodic checkpoint compactor for the Capsule Manifest Chain (cap-001, FR-03).

Every ``checkpoint_every`` commits, emits ``<v>.checkpoint.ndjson`` under
``_capsule_log/<tenant>/<run_id>/``.  The file is NDJSON:

    line 0: CheckpointHeader JSON (type="checkpoint_header")
    lines 1..N: ManifestCommit JSON objects (versions 1..checkpoint_version)

Replay invariant (FR-03):
    Replaying ``(checkpoint @ vK) + commits (vK+1..vN)`` MUST be
    byte-equivalent to replaying ``v1..vN`` from scratch.

This is guaranteed because:
- The checkpoint file contains the canonical ``model_dump_json()`` bytes for
  each commit.
- The chain writer uses the same serialisation for individual commit files.
- Replay concatenates checkpoint + delta commits using the same sort order.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from novafabric.object_capsule_store.manifest_chain import ManifestChainWriter, _chain_prefix
from novafabric.object_capsule_store.models import ManifestCommit
from novafabric.object_capsule_store.worm.base import WormAdapter

log = logging.getLogger(__name__)

_DEFAULT_CHECKPOINT_EVERY = 1_000


@dataclass
class CheckpointResult:
    tenant: str
    run_id: str
    checkpoint_version: int
    commit_count: int
    key: str


def _checkpoint_key(tenant: str, run_id: str, version: int) -> str:
    return f"_capsule_log/{tenant}/{run_id}/{version:010d}.checkpoint.ndjson"


def _build_ndjson(header: dict[str, object], commits: list[ManifestCommit]) -> bytes:
    lines = [json.dumps(header, separators=(",", ":")).encode("utf-8")]
    for commit in commits:
        lines.append(commit.model_dump_json().encode("utf-8"))
    return b"\n".join(lines)


class CheckpointCompactor:
    """Writes periodic checkpoint files for a single (tenant, run_id) chain.

    Args:
        adapter:          WormAdapter instance (chain objects are NOT WORM-locked).
        checkpoint_every: Emit a checkpoint every N commits (default 1,000).
    """

    def __init__(
        self,
        adapter: WormAdapter,
        checkpoint_every: int = _DEFAULT_CHECKPOINT_EVERY,
    ) -> None:
        self._adapter = adapter
        self._checkpoint_every = checkpoint_every
        self._chain_writer = ManifestChainWriter(adapter)

    def maybe_checkpoint(self, tenant: str, run_id: str) -> CheckpointResult | None:
        """Emit a checkpoint if the chain length is a multiple of ``checkpoint_every``.

        Returns the ``CheckpointResult`` if a checkpoint was written, or
        ``None`` if no checkpoint was needed.
        """
        commits = self._chain_writer.read_chain(tenant, run_id)
        if not commits:
            return None
        last_version = commits[-1].version
        if last_version % self._checkpoint_every != 0:
            return None
        return self._write_checkpoint(tenant, run_id, commits)

    def force_checkpoint(self, tenant: str, run_id: str) -> CheckpointResult | None:
        """Write a checkpoint unconditionally (used by tests and admin tooling)."""
        commits = self._chain_writer.read_chain(tenant, run_id)
        if not commits:
            return None
        return self._write_checkpoint(tenant, run_id, commits)

    def _write_checkpoint(
        self, tenant: str, run_id: str, commits: list[ManifestCommit]
    ) -> CheckpointResult:
        last_version = commits[-1].version
        first_version = commits[0].version
        header: dict[str, object] = {
            "type": "checkpoint_header",
            "checkpoint_version": last_version,
            "tenant": tenant,
            "run_id": run_id,
            "created_at_ms": int(time.time() * 1000),
            "commit_count": len(commits),
            "first_version": first_version,
            "last_version": last_version,
        }
        ndjson = _build_ndjson(header, commits)
        key = _checkpoint_key(tenant, run_id, last_version)
        self._adapter.put_log_object(key, ndjson)
        log.info(
            "Checkpoint written: tenant=%s run_id=%s version=%d commits=%d",
            tenant, run_id, last_version, len(commits),
        )
        return CheckpointResult(
            tenant=tenant,
            run_id=run_id,
            checkpoint_version=last_version,
            commit_count=len(commits),
            key=key,
        )

    # -----------------------------------------------------------------------
    # Replay helpers (FR-03 byte-equivalence contract)
    # -----------------------------------------------------------------------

    def replay(self, tenant: str, run_id: str) -> list[ManifestCommit]:
        """Reconstruct full commit sequence using checkpoint + delta.

        Algorithm:
        1. Find the latest checkpoint file in the chain log prefix.
        2. Load all commits from that checkpoint (lines 1..N).
        3. Load individual commit files with version > checkpoint_version.
        4. Sort and return.

        The result is byte-equivalent to ``read_chain()`` on the raw commits.
        """
        prefix = _chain_prefix(tenant, run_id)
        keys = self._adapter.list_objects(prefix)

        # Find checkpoints (sorted descending to find the latest)
        checkpoint_keys = sorted(
            (k for k in keys if k.endswith(".checkpoint.ndjson")),
            reverse=True,
        )

        if not checkpoint_keys:
            # No checkpoint — full replay from individual commits
            return self._chain_writer.read_chain(tenant, run_id)

        latest_checkpoint_key = checkpoint_keys[0]
        checkpoint_version = int(
            latest_checkpoint_key.split("/")[-1].replace(".checkpoint.ndjson", "")
        )

        # Read commits from checkpoint
        raw = self._adapter.get_object(latest_checkpoint_key)
        lines = raw.split(b"\n")
        # line 0 is the header, lines 1..N are commits
        checkpoint_commits = [
            ManifestCommit.model_validate_json(line)
            for line in lines[1:]
            if line.strip()
        ]

        # Read delta commits (version > checkpoint_version)
        delta_commits = self._chain_writer.read_since(tenant, run_id, checkpoint_version)

        all_commits = checkpoint_commits + delta_commits
        return sorted(all_commits, key=lambda c: c.version)
