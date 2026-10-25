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

    def maybe_checkpoint(
        self, tenant: str, run_id: str, last_version: int | None = None
    ) -> CheckpointResult | None:
        """Emit a checkpoint if the chain length is a multiple of ``checkpoint_every``.

        Pass ``last_version`` (the version of the commit just written) to decide without
        touching the store: the chain is listed and fetched only when a checkpoint is due.
        Without it every put re-read and re-verified the whole run chain, O(n) per write and
        O(n²) per run (review storage-backup-04).

        Returns the ``CheckpointResult`` if a checkpoint was written, or
        ``None`` if no checkpoint was needed.
        """
        if last_version is not None and last_version % self._checkpoint_every != 0:
            return None
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
        """Reconstruct the full commit sequence, trusting only the verified chain.

        See :meth:`replay_checked`; a disagreeing checkpoint is logged and ignored.
        """
        commits, problems = self.replay_checked(tenant, run_id)
        for problem in problems:
            log.warning("%s", problem)
        return commits

    def replay_checked(
        self, tenant: str, run_id: str
    ) -> tuple[list[ManifestCommit], list[str]]:
        """Return the commit sequence from the individual, hash-verified commit files, plus
        a description of every way the latest checkpoint disagrees with them.

        The ``.checkpoint.ndjson`` object is written with ``put_log_object``, so it is NOT
        WORM-locked: anyone with bucket write access can rewrite it. The earlier replay took
        commits 1..K from it without comparing them to the commit files, so editing one
        ``capsule_sha256`` there put a forged pin into the rebuilt metadata DB with no warning,
        although ``read_chain`` had already fetched and verified every one of those files
        (review storage-backup-03). The commit files are the authority; a checkpoint is only
        a cache, so a disagreement is reported and never used. This means a checkpoint saves
        no I/O here — correctness over the old "O(1 checkpoint + delta)" claim.
        """
        chain = self._chain_writer.read_chain(tenant, run_id)
        problems: list[str] = []

        keys = self._adapter.list_objects(_chain_prefix(tenant, run_id))
        checkpoint_keys = sorted(
            (k for k in keys if k.endswith(".checkpoint.ndjson")), reverse=True
        )
        if not checkpoint_keys:
            return chain, problems

        key = checkpoint_keys[0]
        try:
            lines = self._adapter.get_object(key).split(b"\n")
            claimed = [
                ManifestCommit.model_validate_json(line) for line in lines[1:] if line.strip()
            ]
        except Exception as exc:  # noqa: BLE001 — a corrupt checkpoint is reported, not fatal
            problems.append(f"checkpoint {key} is unreadable and was ignored: {exc}")
            return chain, problems

        by_version = {c.version: c for c in chain}
        for commit in claimed:
            real = by_version.get(commit.version)
            if real is None:
                problems.append(
                    f"checkpoint {key} lists version {commit.version}, which has no verified "
                    "commit file; the checkpoint was ignored"
                )
            elif real != commit:
                problems.append(
                    f"checkpoint {key} disagrees with the verified commit file at version "
                    f"{commit.version} (capsule_sha256 {commit.capsule_sha256[:12]} vs "
                    f"{real.capsule_sha256[:12]}); the commit file was used"
                )
        return chain, problems
