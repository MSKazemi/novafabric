"""Restore a ``manifest-only`` backup set against a live WORM bucket (ADR-0216 D6).

The set carries no capsule blobs — the bucket is the durability layer. Restore
therefore means: extract the home-side members (registry, redacted config),
then **prove the bucket still matches the signed listing**: every recorded
chain head must verify as an *ancestor* of the live chain (live chains may
have advanced past the backup — that is normal; a tampered or truncated chain
is not), chain integrity must hold end-to-end, and the metadata DB is rebuilt
from the chain (the ADR-0175 disaster-recovery path). An unreachable bucket
is a distinct failure (exit code 2 at the CLI) — a manifest-only set without
its bucket restores nothing.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path
from typing import Optional

from novafabric.backup.models import (
    RestoreResult,
    RestoreStepResult,
)
from novafabric.backup.object_store_manifest import (
    OBJECT_STORE_MANIFEST_NAME,
    BackendFingerprint,
    ObjectStoreListing,
    commit_sha256,
)
from novafabric.backup.restore import (
    RestoreError,
    _extract_members,
    _prepare_home,
    _replay_crypto_shreds,
    _run_migrations,
    _verify_set,
    _verify_state_dbs,
)
from novafabric.backup.verify import read_manifest
from novafabric.object_capsule_store.worm.base import WormAdapter


class BucketUnreachableError(RestoreError):
    """The listed object-store bucket cannot be reached — CLI exit code 2."""


def restore_manifest_backup(
    set_path: Path,
    *,
    home: Path,
    adapter: WormAdapter,
    force: bool = False,
    decision_log_path: Optional[Path] = None,
    audit_log_path: Optional[Path] = None,
    sample: int = 0,
) -> RestoreResult:
    """Restore the manifest-only set at *set_path*, verifying against *adapter*.

    Args:
        set_path: The ``manifest-only`` backup archive.
        home: Target NovaFabric home for the home-side members and the
            rebuilt metadata DB.
        adapter: Live WORM adapter for the bucket the listing describes.
        force: As in the local restore — never silently overwrite.
        decision_log_path: Shred decision log override (D4 replay).
        audit_log_path: Audit-log destination override.
        sample: Spot-check this many capsule payload hashes via
            ``verify_capsule`` (0 = off; cost honesty — a full sweep of a
            large bucket is a deliberate operator action, not a default).

    Raises:
        BucketUnreachableError: the bucket has none of the listed objects.
        RestoreError: set verification failed / unsafe members / non-empty
            home without force.
    """
    home = home.expanduser()
    steps: list[RestoreStepResult] = []

    # --- 1. verify the set offline ------------------------------------------
    verdict = _verify_set(set_path)
    steps.append(
        RestoreStepResult(
            name="verify-set",
            ok=True,
            detail=(
                f"{len(verdict.ok_members)}/{len(verdict.checks)} members ok; "
                f"signing: {verdict.signing_status}"
            ),
        )
    )
    manifest = read_manifest(set_path)
    listing = _read_listing(set_path)

    # --- 2. resolve-backend: fingerprint sanity (warn, never fail) ----------
    live_fp = _live_fingerprint(adapter, listing.backend_fingerprint)
    if live_fp.fingerprint_sha256 != listing.backend_fingerprint.fingerprint_sha256:
        fp_detail = (
            "WARNING: live backend fingerprint differs from the listing "
            f"({live_fp.backend_tag!r} vs "
            f"{listing.backend_fingerprint.backend_tag!r}) — proceeding, but "
            "confirm this is the intended bucket (migrated buckets are "
            "legitimate; wrong buckets fail chain verification below)"
        )
    else:
        fp_detail = f"backend fingerprint matches ({live_fp.backend_tag})"
    steps.append(RestoreStepResult(name="resolve-backend", ok=True, detail=fp_detail))

    # --- 3. probe-bucket: unreachable = hard, distinct failure ---------------
    steps.append(_probe_bucket(adapter, listing))

    # --- 4. home-side members (registry snapshot, redacted config) ----------
    home_members = [
        m for m in manifest.members
        if m.origin == "home" and m.path != OBJECT_STORE_MANIFEST_NAME
    ]
    moved_aside = _prepare_home(home, [m.path for m in home_members], force=force)
    steps.append(
        RestoreStepResult(
            name="prepare-home",
            ok=True,
            detail=(
                f"pre-existing data moved to {moved_aside}"
                if moved_aside
                else f"target home {home} ready"
            ),
        )
    )
    extracted = _extract_members(set_path, home, home_members, {})
    steps.append(
        RestoreStepResult(
            name="extract", ok=True, detail=f"{extracted} home member(s) extracted"
        )
    )
    steps.append(_run_migrations(home))

    # --- 5. verify every pinned chain head against the live bucket -----------
    steps.append(_verify_chains(adapter, listing))

    # --- 6. rebuild the metadata DB from the chain (ADR-0175 DR path) --------
    steps.append(_rebuild_metadata(adapter, listing, home))

    # --- 7. optional capsule spot-checks -------------------------------------
    if sample > 0:
        steps.append(_sample_capsules(adapter, listing, sample))

    # --- 8. shred replay + state verification --------------------------------
    from novafabric.backup.coverage import default_audit_log_path

    log = decision_log_path or audit_log_path or default_audit_log_path()
    steps.append(_replay_crypto_shreds(home, log))
    steps.append(_verify_state_dbs(home, home_members))

    return RestoreResult(
        ok=all(step.ok for step in steps),
        set_id=verdict.set_id,
        profile=verdict.profile,
        home=str(home),
        steps=steps,
        moved_aside=str(moved_aside) if moved_aside else None,
    )


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def _read_listing(set_path: Path) -> ObjectStoreListing:
    with tarfile.open(set_path, "r:gz") as tar:
        fh = tar.extractfile(OBJECT_STORE_MANIFEST_NAME)
        if fh is None:
            raise RestoreError(
                f"manifest-only set has no {OBJECT_STORE_MANIFEST_NAME} member"
            )
        with fh:
            return ObjectStoreListing.model_validate(json.loads(fh.read()))


def _live_fingerprint(
    adapter: WormAdapter, recorded: BackendFingerprint
) -> BackendFingerprint:
    """The live side reuses the recorded coordinates but the real adapter tag.

    Endpoint/bucket/etc. are constructor-time facts not readable back from
    every adapter; the adapter class is the one live fact always available.
    """
    tag = type(adapter).__name__.replace("WormAdapter", "").lower() or "unknown"
    if tag == "inmemory":
        tag = "local"
    return recorded.model_copy(update={"backend_tag": tag}).with_hash()


def _probe_bucket(adapter: WormAdapter, listing: ObjectStoreListing) -> RestoreStepResult:
    name = "probe-bucket"
    probe_keys: list[str] = []
    for tenant in listing.tenants:
        for run in tenant.runs:
            probe_keys.append(
                f"_capsule_log/{tenant.tenant}/{run.run_id}/"
                f"{run.head_version:010d}.json"
            )
            if len(probe_keys) >= 3:
                break
        if len(probe_keys) >= 3:
            break
    if not probe_keys:
        return RestoreStepResult(
            name=name, ok=True, detail="listing records no chains — nothing to probe"
        )
    try:
        reachable = any(adapter.object_exists(k) for k in probe_keys)
    except Exception as exc:  # noqa: BLE001 — network/auth failures land here
        raise BucketUnreachableError(
            f"Cannot reach the object store the listing describes: {exc}. "
            "Point restore at the right backend (--backend / NOVA_OCS_BACKEND)."
        ) from exc
    if not reachable:
        raise BucketUnreachableError(
            "None of the listed chain heads exist in the reachable bucket — "
            "this is not the bucket the backup describes (or it lost the "
            "chain log). Point restore at the right backend "
            "(--backend / NOVA_OCS_BACKEND)."
        )
    return RestoreStepResult(
        name=name, ok=True, detail=f"bucket reachable ({len(probe_keys)} head(s) probed)"
    )


def _verify_chains(adapter: WormAdapter, listing: ObjectStoreListing) -> RestoreStepResult:
    """Every recorded head must verify as an ANCESTOR of the live chain."""
    from novafabric.object_capsule_store.exceptions import ChainIntegrityError
    from novafabric.object_capsule_store.manifest_chain import ManifestChainWriter

    name = "verify-chains"
    writer = ManifestChainWriter(adapter)
    verified = 0
    advanced = 0
    failures: list[str] = []
    for tenant in listing.tenants:
        for run in tenant.runs:
            label = f"{tenant.tenant}/{run.run_id}"
            try:
                commits = writer.read_chain(
                    tenant.tenant, run.run_id, verify_integrity=True
                )
            except ChainIntegrityError as exc:
                failures.append(f"{label}: chain integrity broken ({exc})")
                continue
            by_version = {c.version: c for c in commits}
            head = by_version.get(run.head_version)
            if head is None:
                failures.append(
                    f"{label}: recorded head v{run.head_version} missing from live chain"
                )
                continue
            if commit_sha256(head) != run.head_commit_sha256:
                failures.append(
                    f"{label}: head v{run.head_version} hash mismatch — "
                    "live chain history differs from the signed listing"
                )
                continue
            verified += 1
            if max(by_version) > run.head_version:
                advanced += 1
    if failures:
        return RestoreStepResult(
            name=name, ok=False, detail="; ".join(failures[:5])
        )
    detail = f"{verified} chain(s) verified against the live bucket"
    if advanced:
        detail += f" ({advanced} advanced past the backup — normal for append-only)"
    return RestoreStepResult(name=name, ok=True, detail=detail)


def _rebuild_metadata(
    adapter: WormAdapter, listing: ObjectStoreListing, home: Path
) -> RestoreStepResult:
    from novafabric.object_capsule_store.rebuild import rebuild_metadata_db

    name = "rebuild-metadata"
    target = home / "nova-metadata-rebuild.db"
    total = 0
    warnings: list[str] = []
    for tenant in listing.tenants:
        report = rebuild_metadata_db(adapter, tenant.tenant, target_db=str(target))
        total += report.capsules_found
        warnings.extend(report.integrity_warnings)
    if warnings:
        return RestoreStepResult(
            name=name,
            ok=False,
            detail=(
                f"{total} capsule(s) rebuilt but {len(warnings)} integrity "
                f"warning(s): {'; '.join(warnings[:3])}"
            ),
        )
    return RestoreStepResult(
        name=name, ok=True, detail=f"{total} capsule(s) rebuilt into {target.name}"
    )


def _sample_capsules(
    adapter: WormAdapter, listing: ObjectStoreListing, sample: int
) -> RestoreStepResult:
    from novafabric.object_capsule_store.manifest_chain import ManifestChainWriter
    from novafabric.object_capsule_store.rebuild import verify_capsule

    name = "verify-capsule-sample"
    writer = ManifestChainWriter(adapter)
    checked = 0
    failures: list[str] = []
    for tenant in listing.tenants:
        for run in tenant.runs:
            if checked >= sample:
                break
            commits = writer.read_chain(tenant.tenant, run.run_id, verify_integrity=False)
            for commit in commits:
                if checked >= sample:
                    break
                if commit.operation != "put":
                    continue
                result = verify_capsule(adapter, commit.capsule_uri, commit.capsule_sha256)
                checked += 1
                if not result.ok:
                    failures.append(f"{commit.capsule_uri}: {result.error}")
        if checked >= sample:
            break
    if failures:
        return RestoreStepResult(name=name, ok=False, detail="; ".join(failures[:3]))
    return RestoreStepResult(
        name=name, ok=True, detail=f"{checked} capsule payload(s) spot-checked ok"
    )
