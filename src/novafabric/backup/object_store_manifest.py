"""Object-store listing for the ``manifest-only`` backup profile (ADR-0216 D6).

For WORM/object-store deployments the capsule blobs are immutable in the
bucket — the backup set records what must be there and how to prove it: per
(tenant, run) the chain **head version and head-commit hash** (hash-chain
linkage makes one pinned head cover every ancestor — the countermeasure for
chain-log objects not being WORM-locked), the newest checkpoint reference,
and commit counts, plus a **secret-free** backend fingerprint and the local
WAL drain state. Collection is shallow by default: O(runs) GETs (head commit
+ checkpoint bytes), one LIST stream over ``_capsule_log/``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict

from novafabric.object_capsule_store.manifest_chain import (
    ManifestChainWriter,
    _version_key,
)
from novafabric.object_capsule_store.models import ManifestCommit
from novafabric.object_capsule_store.worm.base import WormAdapter

LISTING_SCHEMA_VERSION = "0.1.0"

#: In-archive name of the listing member.
OBJECT_STORE_MANIFEST_NAME = "object_store_manifest.json"

_CHAIN_ROOT = "_capsule_log/"

#: Fingerprint fields — non-secret backend coordinates ONLY. Never DSNs,
#: tokens, access keys, or KEK paths (ADR-0181 D3 hygiene).
_FINGERPRINT_FIELDS = (
    "backend_tag",
    "endpoint_host",
    "bucket",
    "region",
    "prefix",
    "encryption_enabled",
)


class ObjectStoreListingError(Exception):
    """Raised when the listing cannot be collected."""


class BackendFingerprint(BaseModel):
    """Secret-free backend coordinates + their canonical hash."""

    model_config = ConfigDict(extra="forbid")

    backend_tag: str
    endpoint_host: Optional[str] = None
    bucket: Optional[str] = None
    region: Optional[str] = None
    prefix: Optional[str] = None
    encryption_enabled: bool = False
    fingerprint_sha256: str = ""

    def with_hash(self) -> "BackendFingerprint":
        body = {f: getattr(self, f) for f in _FINGERPRINT_FIELDS}
        digest = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return self.model_copy(update={"fingerprint_sha256": digest})


class CheckpointRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    checkpoint_version: int
    sha256: str


class RunChainHead(BaseModel):
    """The pinned head of one (tenant, run) chain."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    head_version: int
    #: SHA-256 of the head commit's canonical ``model_dump_json()`` — the same
    #: convention as ``prev_commit_hash``, so the whole chain is pinned.
    head_commit_sha256: str
    commit_count: int
    latest_checkpoint: Optional[CheckpointRef] = None


class TenantEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant: str
    runs: list[RunChainHead]


class WalState(BaseModel):
    """Local-WAL drain state at listing time — honest evidence of any gap."""

    model_config = ConfigDict(extra="forbid")

    present: bool = False
    pending: int = 0
    poisoned: int = 0


class ObjectStoreListing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    listing_schema_version: str = LISTING_SCHEMA_VERSION
    generated_at: str
    backend_fingerprint: BackendFingerprint
    tenants: list[TenantEntry]
    totals: dict[str, int]
    wal_state: WalState = WalState()
    #: Shallow (default) reads only heads + checkpoint bytes; deep ran a full
    #: read_chain(verify_integrity=True) per run at create time.
    deep_verified: bool = False


def commit_sha256(commit: ManifestCommit) -> str:
    """Canonical commit hash — MUST match manifest_chain's prev_commit_hash."""
    return hashlib.sha256(commit.model_dump_json().encode("utf-8")).hexdigest()


def collect_object_store_listing(
    adapter: WormAdapter,
    fingerprint: BackendFingerprint,
    *,
    tenants: Optional[list[str]] = None,
    deep: bool = False,
    wal_state: Optional[WalState] = None,
) -> ObjectStoreListing:
    """Stream ``_capsule_log/`` and pin every chain head.

    Args:
        adapter: The WORM adapter to enumerate.
        fingerprint: Secret-free backend coordinates (hash filled here).
        tenants: Optional scope — only these tenants are listed (R1: tenant
            enumeration at scale).
        deep: Also run ``read_chain(verify_integrity=True)`` per run.
        wal_state: Local WAL drain state, recorded verbatim.
    """
    per_tenant: dict[str, dict[str, dict[str, list[int]]]] = {}
    for key in adapter.iter_objects(_CHAIN_ROOT):
        parts = key.split("/")
        if len(parts) != 4 or parts[0] != "_capsule_log":
            continue
        _, tenant, run_id, fname = parts
        if tenants is not None and tenant not in tenants:
            continue
        bucket = per_tenant.setdefault(tenant, {}).setdefault(
            run_id, {"commits": [], "checkpoints": []}
        )
        if fname.endswith(".checkpoint.ndjson"):
            stem = fname.replace(".checkpoint.ndjson", "")
            kind = "checkpoints"
        elif fname.endswith(".json"):
            stem = fname.replace(".json", "")
            kind = "commits"
        else:
            continue
        try:
            bucket[kind].append(int(stem))
        except ValueError:
            continue

    entries: list[TenantEntry] = []
    total_runs = 0
    total_commits = 0
    writer = ManifestChainWriter(adapter)
    for tenant in sorted(per_tenant):
        heads: list[RunChainHead] = []
        for run_id in sorted(per_tenant[tenant]):
            versions = sorted(per_tenant[tenant][run_id]["commits"])
            if not versions:
                continue
            head_version = versions[-1]
            head_raw = adapter.get_object(_version_key(tenant, run_id, head_version))
            head_commit = ManifestCommit.model_validate_json(head_raw)
            checkpoint_ref: Optional[CheckpointRef] = None
            checkpoints = sorted(per_tenant[tenant][run_id]["checkpoints"])
            if checkpoints:
                ckpt_version = checkpoints[-1]
                ckpt_key = (
                    f"_capsule_log/{tenant}/{run_id}/"
                    f"{ckpt_version:010d}.checkpoint.ndjson"
                )
                checkpoint_ref = CheckpointRef(
                    key=ckpt_key,
                    checkpoint_version=ckpt_version,
                    sha256=hashlib.sha256(adapter.get_object(ckpt_key)).hexdigest(),
                )
            if deep:
                writer.read_chain(tenant, run_id, verify_integrity=True)
            heads.append(
                RunChainHead(
                    run_id=run_id,
                    head_version=head_version,
                    head_commit_sha256=commit_sha256(head_commit),
                    commit_count=len(versions),
                    latest_checkpoint=checkpoint_ref,
                )
            )
            total_runs += 1
            total_commits += len(versions)
        entries.append(TenantEntry(tenant=tenant, runs=heads))

    return ObjectStoreListing(
        generated_at=datetime.now(timezone.utc).isoformat(),
        backend_fingerprint=fingerprint.with_hash(),
        tenants=entries,
        totals={
            "tenants": len(entries),
            "runs": total_runs,
            "commits": total_commits,
        },
        wal_state=wal_state or WalState(),
        deep_verified=deep,
    )
