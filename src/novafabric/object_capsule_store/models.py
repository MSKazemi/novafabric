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
"""Pydantic v2 models for the Object Capsule Store (Phase 4 — cap-001..cap-004).

Cross-study public contract:
- ``ManifestCommit`` is the inter-study API consumed by metadata-database (Phase 5)
  and lineage-at-scale (Phase 6).  Additions to the ``stats`` field are
  backwards-compatible; no field may be removed or renamed.
- ``CapsuleReceipt`` is returned to upstream collectors (Phase 2) and spans
  carried in Event Envelope v1 trace context.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# ManifestCommit — public inter-study API (do NOT break without a new ADR)
# ---------------------------------------------------------------------------

class ManifestCommit(BaseModel):
    """One immutable JSON commit in the per-run chain log (FR-01).

    Schema: schemas/manifest_commit_v1.schema.json
    Written to: _capsule_log/<tenant>/<run_id>/<version>.json
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(..., ge=1, description="Monotonically increasing per-(tenant,run_id)")
    timestamp_ms: int = Field(..., description="Unix epoch milliseconds (UTC)")
    operation: str = Field(..., description="'put' | 'tombstone'")
    capsule_uri: str = Field(
        ..., description="Canonical CAS key: capsules/<t>/<s[:4]>/<sha>/data.zst"
    )
    capsule_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    run_id: str
    tenant: str
    novaseal_leaf_hash: str | None = Field(
        None, description="SHA-256 hex of the NovaSeal leaf entry; null if PENDING_SIGNATURE"
    )
    signed_at: str | None = Field(None, description="ISO-8601 UTC timestamp of NovaSeal signing")
    stats: dict[str, Any] = Field(
        default_factory=dict, description="Extension block; additions are non-breaking"
    )
    compression_dict_id: str | None = Field(
        None, description="Zstd dictionary ID used for compression; null = no dict"
    )
    prev_commit_hash: str | None = Field(
        None,
        description=(
            "SHA-256 hex of the immediately preceding commit's canonical JSON "
            "(model_dump_json()).  Null for version==1 (genesis commit).  "
            "Optional in old commits (pre-OQ-027); present in all new commits."
        ),
    )


# ---------------------------------------------------------------------------
# Receipt status
# ---------------------------------------------------------------------------

class ReceiptStatus(str, Enum):
    OK = "ok"
    PENDING_SIGNATURE = "pending_signature"
    ERROR = "error"


# ---------------------------------------------------------------------------
# CapsuleReceipt — returned to callers of put_capsule()
# ---------------------------------------------------------------------------

class CapsuleReceipt(BaseModel):
    """Result returned by ``ObjectCapsuleStore.put_capsule()``."""

    model_config = ConfigDict(extra="forbid")

    capsule_uri: str
    capsule_sha256: str
    version: int | None = None
    status: ReceiptStatus = ReceiptStatus.OK
    novaseal_leaf_hash: str | None = None
    wal_row_id: int | None = Field(
        None, description="Local WAL row id when status=PENDING_SIGNATURE"
    )


# ---------------------------------------------------------------------------
# OrphanReport — returned by reconcile_orphans()
# ---------------------------------------------------------------------------

class OrphanReport(BaseModel):
    """Summary of orphan-reconciliation scan (FR-09)."""

    model_config = ConfigDict(extra="forbid")

    scanned: int = 0
    orphans_found: int = 0
    tombstones_written: int = 0
    dry_run: bool = True
    errors: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# IntegrityResult — returned by verify_capsule()
# ---------------------------------------------------------------------------

class IntegrityResult(BaseModel):
    """Result of off-path integrity verification (FR-16)."""

    model_config = ConfigDict(extra="forbid")

    capsule_uri: str
    ok: bool
    expected_sha256: str | None = None
    observed_sha256: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# RebuildReport — returned by rebuild_metadata_db()
# ---------------------------------------------------------------------------

class RebuildReport(BaseModel):
    """Summary of metadata-DB rebuild from chain log (FR-04)."""

    model_config = ConfigDict(extra="forbid")

    capsules_found: int = 0
    time_to_replay_seconds: float = 0.0
    integrity_warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# WAL entry (internal — not a cross-study contract)
# ---------------------------------------------------------------------------

class WalEntry(BaseModel):
    """Row persisted to the Local WAL when NovaSeal is unavailable (adr-002)."""

    model_config = ConfigDict(extra="forbid")

    capsule_uri: str
    capsule_sha256: str
    tenant: str
    run_id: str
    uploaded_at: str = Field(description="ISO-8601 UTC")
