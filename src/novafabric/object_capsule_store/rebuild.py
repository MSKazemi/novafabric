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
"""Rebuild, reconcile, and verify operations for the Object Capsule Store.

FR-04: rebuild_metadata_db() — scan chain log and rebuild metadata DB.
FR-09: reconcile_orphans()   — find data objects without chain commits.
FR-16: verify_capsule()      — off-path integrity check.
"""

from __future__ import annotations

import logging
import time

from novafabric.object_capsule_store.cas import compute_sha256
from novafabric.object_capsule_store.checkpoint import CheckpointCompactor
from novafabric.object_capsule_store.manifest_chain import ManifestChainWriter
from novafabric.object_capsule_store.models import (
    IntegrityResult,
    OrphanReport,
    RebuildReport,
)
from novafabric.object_capsule_store.worm.base import WormAdapter

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# rebuild_metadata_db (FR-04)
# ---------------------------------------------------------------------------

def rebuild_metadata_db(
    adapter: WormAdapter,
    prefix: str,
    target_db: str = ":memory:",
) -> RebuildReport:
    """Scan ``_capsule_log/<prefix>`` and rebuild metadata from chain alone.

    This is the disaster-recovery path: if the metadata database is lost,
    the chain log is the source of truth.

    Args:
        adapter:   WormAdapter for reading chain-log objects.
        prefix:    Tenant (or ``tenant/run_id``) prefix to scan.
        target_db: Path to target SQLite database (default: ``:memory:``).

    Returns:
        ``RebuildReport`` with counts and timing.
    """
    import sqlite3

    start = time.monotonic()
    warnings: list[str] = []
    capsules_found = 0

    conn = sqlite3.connect(target_db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS capsules (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant          TEXT NOT NULL,
            run_id          TEXT NOT NULL,
            capsule_uri     TEXT NOT NULL UNIQUE,
            capsule_sha256  TEXT NOT NULL,
            version         INTEGER NOT NULL,
            timestamp_ms    INTEGER NOT NULL,
            novaseal_leaf   TEXT,
            signed_at       TEXT,
            operation       TEXT NOT NULL DEFAULT 'put'
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_capsules_run ON capsules (tenant, run_id)
    """)
    conn.commit()

    # Discover unique (tenant, run_id) pairs by STREAMING the chain-log keys
    # (ADR-0175). `iter_objects` yields page-by-page from the backend paginator,
    # so peak memory is bounded by the number of distinct (tenant, run_id) pairs
    # rather than the total key count — safe for namespaces with millions of
    # capsules (OQ-028). Checkpoint files remain the fast read path.
    log_prefix = f"_capsule_log/{prefix}"

    pairs: set[tuple[str, str]] = set()
    for key in adapter.iter_objects(log_prefix):
        parts = key.split("/")
        if len(parts) >= 4:
            pairs.add((parts[1], parts[2]))

    # Per-(tenant, run_id): replay via checkpoint + delta (AC-1).
    # The CheckpointCompactor.replay() method reads the latest .checkpoint.ndjson
    # (all historic commits in one object) and then only fetches individual files
    # for the delta since the last checkpoint.  This reduces I/O from O(N commits)
    # to O(1 checkpoint read + delta reads) and completes in minutes, not hours.
    compactor = CheckpointCompactor(adapter)
    for (tenant, run_id) in sorted(pairs):
        try:
            commits = compactor.replay(tenant, run_id)
        except Exception as exc:
            warnings.append(f"Replay error for {tenant}/{run_id}: {exc}")
            continue

        for commit in commits:
            if commit.operation != "put":
                continue
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO capsules
                    (tenant, run_id, capsule_uri, capsule_sha256, version,
                     timestamp_ms, novaseal_leaf, signed_at, operation)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        commit.tenant, commit.run_id, commit.capsule_uri,
                        commit.capsule_sha256, commit.version, commit.timestamp_ms,
                        commit.novaseal_leaf_hash, commit.signed_at, commit.operation,
                    ),
                )
                capsules_found += 1
            except Exception as exc:
                warnings.append(f"DB insert error for {commit.capsule_uri}: {exc}")

    conn.commit()
    conn.close()

    elapsed = time.monotonic() - start
    log.info(
        "rebuild_metadata_db complete: prefix=%s capsules=%d time=%.2fs warnings=%d",
        prefix, capsules_found, elapsed, len(warnings),
    )
    return RebuildReport(
        capsules_found=capsules_found,
        time_to_replay_seconds=elapsed,
        integrity_warnings=warnings,
    )


# ---------------------------------------------------------------------------
# reconcile_orphans (FR-09)
# ---------------------------------------------------------------------------

def reconcile_orphans(
    adapter: WormAdapter,
    tenant: str,
    dry_run: bool = True,
) -> OrphanReport:
    """Scan for data objects without a chain commit (orphans).

    An "orphan" is a data object at ``capsules/<tenant>/...`` that has no
    corresponding chain commit in ``_capsule_log/<tenant>/...``.

    If ``dry_run=False`` and the retention has expired (not possible to
    determine without backend metadata — assumed always non-expired here for
    safety), appends a chain-level tombstone.  NEVER hard-deletes WORM objects.

    Args:
        adapter:  WormAdapter for reading.
        tenant:   Tenant to scan.
        dry_run:  If True (default), report only; do not write tombstones.

    Returns:
        ``OrphanReport``.
    """
    report = OrphanReport(dry_run=dry_run)
    chain_writer = ManifestChainWriter(adapter)

    # Collect all committed capsule URIs for this tenant
    all_chain_keys = adapter.list_objects(f"_capsule_log/{tenant}/")
    committed_uris: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    for key in all_chain_keys:
        parts = key.split("/")
        if len(parts) >= 4:
            pairs.add((parts[1], parts[2]))

    for t, run_id in pairs:
        try:
            commits = chain_writer.read_chain(t, run_id)
            for c in commits:
                committed_uris.add(c.capsule_uri)
        except Exception as exc:
            report.errors.append(f"Chain read error {t}/{run_id}: {exc}")

    # List all data objects under capsules/<tenant>/
    data_keys = adapter.list_objects(f"capsules/{tenant}/")
    data_keys = [k for k in data_keys if k.endswith("/data.zst")]

    report.scanned = len(data_keys)
    for key in data_keys:
        if key not in committed_uris:
            report.orphans_found += 1
            log.warning("Orphan found: %s", key)
            if not dry_run:
                # Derive (run_id) is unknown for orphans — use a sentinel run_id
                # Extract sha256 from key: capsules/<tenant>/<sha4>/<sha256>/data.zst
                parts = key.split("/")
                if len(parts) == 5:
                    sha256 = parts[3]
                    try:
                        chain_writer.append_tombstone(
                            tenant=tenant,
                            run_id="_orphan_reconciliation",
                            capsule_uri=key,
                            capsule_sha256=sha256,
                        )
                        report.tombstones_written += 1
                    except Exception as exc:
                        report.errors.append(f"Tombstone error for {key}: {exc}")

    return report


# ---------------------------------------------------------------------------
# verify_capsule (FR-16)
# ---------------------------------------------------------------------------

def verify_capsule(
    adapter: WormAdapter,
    capsule_uri: str,
    expected_sha256: str,
) -> IntegrityResult:
    """Off-path integrity check for a single capsule (FR-16).

    Downloads the capsule data, computes SHA-256, and compares against
    *expected_sha256*.  Does NOT touch the hot write path.

    Args:
        adapter:         WormAdapter for reading.
        capsule_uri:     Canonical CAS key (``capsules/<t>/<sha4>/<sha>/data.zst``).
        expected_sha256: Expected SHA-256 hex.

    Returns:
        ``IntegrityResult(ok=True)`` if healthy; ``ok=False`` with
        ``observed_sha256`` populated if tampered.
    """
    try:
        data = adapter.get_object(capsule_uri)
    except Exception as exc:
        return IntegrityResult(
            capsule_uri=capsule_uri,
            ok=False,
            expected_sha256=expected_sha256,
            error=f"Failed to download: {exc}",
        )

    observed = compute_sha256(data)
    if observed == expected_sha256:
        return IntegrityResult(
            capsule_uri=capsule_uri,
            ok=True,
            expected_sha256=expected_sha256,
            observed_sha256=observed,
        )
    return IntegrityResult(
        capsule_uri=capsule_uri,
        ok=False,
        expected_sha256=expected_sha256,
        observed_sha256=observed,
        error="SHA-256 mismatch — capsule may have been tampered with",
    )
