"""Restore a local-profile backup set (ADR-0181 second slice, experimental).

``restore_backup`` implements the spec's normative restore order
(``design/spec/backup-restore-v0.md`` §Restore procedure) for the LOCAL
(SQLite) profile:

1. **Verify the set first** — a set that fails ``verify_backup`` is refused
   before the target home is touched. There is deliberately no way to skip
   this (a restore that is not verified is not a restore).
2. **Prepare the target home** — a non-empty home is refused unless ``force``,
   and even then nothing is silently clobbered: every path the set would
   overwrite is moved into a timestamped ``.pre-restore-…/`` directory first.
3. **Extract members** — path-traversal-safe: only paths listed in the
   manifest are extracted, absolute and ``..`` paths are rejected, and the
   normative key-material deny-filter applies again (defense in depth).
4. **Migrations** — the registry bootstrap DDL is idempotent and additive, so
   it is re-applied programmatically to bring an older snapshot to head.
5. **Crypto-shred replay (NORMATIVE, ADR-0181 D4)** — every applied
   ``CRYPTO_SHRED`` record in the retention decision log is re-applied against
   the restored DEK store: a DEK the log says was destroyed is destroyed again
   if the backup (or surrounding state) resurrected it. Shredded stays
   shredded, deterministically, before the restore is declared done.
6. **Verification chain** — the doctor storage check and, when a Merkle log
   exists under the home, ``seal log verify`` — run programmatically. The
   restore is complete ONLY when verification passes (ADR-0181 D6).

Restore of the ``pg-dump`` profile is NOT implemented in this slice — that is
the ``pg_restore`` runbook (``docs/ops/backup-restore.md`` §1.2), stated
honestly rather than half-automated.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional

from novafabric.audit import AUDIT_LOG_PATH
from novafabric.backup.create import is_denied
from novafabric.backup.models import (
    PROFILE_LOCAL_FULL,
    RestoreResult,
    RestoreStepResult,
    VerifyResult,
)
from novafabric.backup.verify import BackupVerifyError, verify_backup

#: Where displaced pre-existing data goes under the home when ``force`` is used.
_PRE_RESTORE_PREFIX = ".pre-restore-"

#: Retention decision-log event type carrying RetentionActionRecord payloads.
_RETENTION_EVENT = "retention.action"


class RestoreError(Exception):
    """Raised when a restore cannot proceed (the target home is left untouched
    for pre-extraction failures)."""


def restore_backup(
    set_path: Path,
    *,
    home: Path,
    force: bool = False,
    decision_log_path: Optional[Path] = None,
) -> RestoreResult:
    """Restore the local-profile backup set at *set_path* into *home*.

    Args:
        set_path: Backup-set archive (``.tar.gz``) produced by ``create_backup``.
        home: Target NovaFabric home directory.
        force: Allow restoring into a non-empty home. Displaced data is moved
            into a timestamped ``.pre-restore-…/`` directory, never deleted.
        decision_log_path: Retention decision log (hash-chained audit JSONL)
            to replay crypto-shreds from. Defaults to the deployment's audit
            log path.

    Returns:
        A :class:`RestoreResult`; ``ok`` is True only when every step —
        including the closing verification chain — passed.

    Raises:
        RestoreError: verification of the set failed, the profile is not
            restorable in this slice, the home is non-empty without *force*,
            or a member path is unsafe. In all these cases the target home
            has not been modified.
    """
    home = home.expanduser()
    steps: list[RestoreStepResult] = []

    # --- 1. verify the set first (abort before touching the home) -----------
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

    if verdict.profile != PROFILE_LOCAL_FULL:
        raise RestoreError(
            f"Set {verdict.set_id} has profile {verdict.profile!r} — only the "
            f"{PROFILE_LOCAL_FULL!r} profile is restorable in this slice. For a "
            "pg-dump set, use the pg_restore runbook "
            "(docs/ops/backup-restore.md §1.2)."
        )

    member_paths = [check.path for check in verdict.checks]
    for path in member_paths:
        _reject_unsafe_member(path)

    # --- 2. prepare the target home -----------------------------------------
    moved_aside = _prepare_home(home, member_paths, force=force)
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

    # --- 3. extract members (manifest-listed paths only) --------------------
    extracted = _extract_members(set_path, home, member_paths)
    steps.append(
        RestoreStepResult(
            name="extract", ok=True, detail=f"{extracted} member(s) extracted"
        )
    )

    # --- 4. migrations to head ----------------------------------------------
    steps.append(_run_migrations(home))

    # --- 5. NORMATIVE crypto-shred replay (ADR-0181 D4) ----------------------
    steps.append(
        _replay_crypto_shreds(
            home, decision_log_path if decision_log_path is not None else AUDIT_LOG_PATH
        )
    )

    # --- 6. verification chain (restore complete ONLY when this passes) -----
    steps.append(_verify_storage(home))
    steps.append(_verify_seal_log(home))

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

def _verify_set(set_path: Path) -> VerifyResult:
    try:
        verdict = verify_backup(set_path)
    except BackupVerifyError as exc:
        raise RestoreError(f"Backup set cannot be read: {exc}") from exc
    if not verdict.ok:
        problems = [f"{c.status}: {c.path}" for c in verdict.mismatched + verdict.missing]
        problems.extend(verdict.errors)
        if verdict.signature_verified is False:
            problems.append("DSSE signature verification failed")
        raise RestoreError(
            f"Backup set {verdict.set_id} failed verification — refusing to "
            f"restore ({'; '.join(problems) or 'unknown error'})"
        )
    return verdict


def _reject_unsafe_member(path: str) -> None:
    pure = PurePosixPath(path)
    if pure.is_absolute() or path.startswith(("/", "\\", "~")) or (
        len(path) > 1 and path[1] == ":"
    ):
        raise RestoreError(f"Unsafe member path in set (absolute): {path!r}")
    if ".." in pure.parts:
        raise RestoreError(f"Unsafe member path in set (traversal): {path!r}")
    if is_denied(pure):
        raise RestoreError(
            f"Set lists excluded path {path!r} — key material must never be "
            "restored from a backup set"
        )


def _prepare_home(home: Path, member_paths: list[str], *, force: bool) -> Optional[Path]:
    """Refuse a non-empty home unless *force*; move aside what would be written."""
    if not home.exists() or not any(home.iterdir()):
        home.mkdir(parents=True, exist_ok=True)
        return None
    if not force:
        raise RestoreError(
            f"Target home {home} is not empty — refusing to overwrite. "
            "Pass --force to move existing data aside into a "
            f"{_PRE_RESTORE_PREFIX}<timestamp>/ directory."
        )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pre_restore = home / f"{_PRE_RESTORE_PREFIX}{timestamp}"
    top_level = {PurePosixPath(p).parts[0] for p in member_paths}
    moved = False
    for name in sorted(top_level):
        existing = home / name
        if existing.exists():
            pre_restore.mkdir(parents=True, exist_ok=True)
            shutil.move(str(existing), str(pre_restore / name))
            moved = True
    return pre_restore if moved else None


def _extract_members(set_path: Path, home: Path, member_paths: list[str]) -> int:
    """Extract exactly the manifest-listed members, safely, as regular files."""
    home_resolved = home.resolve()
    count = 0
    with tarfile.open(set_path, "r:gz") as tar:
        for path in member_paths:
            fh = tar.extractfile(path)
            if fh is None:  # verify guarantees presence; belt-and-suspenders
                raise RestoreError(f"Member {path!r} vanished from the archive")
            dst = home / path
            if not dst.resolve().is_relative_to(home_resolved):
                raise RestoreError(f"Member {path!r} escapes the target home")
            dst.parent.mkdir(parents=True, exist_ok=True)
            with fh, open(dst, "wb") as out:
                shutil.copyfileobj(fh, out)
            count += 1
    return count


def _run_migrations(home: Path) -> RestoreStepResult:
    """Bring the restored registry to schema head via the idempotent bootstrap DDL."""
    registry_db = home / "registry.db"
    if not registry_db.exists():
        return RestoreStepResult(
            name="migrations", ok=True, detail="no registry.db in set — nothing to migrate"
        )
    from novafabric.registry.store import get_connection, init_schema

    try:
        conn = get_connection(registry_db)
        try:
            init_schema(conn)
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            revision = row[0] if row else None
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return RestoreStepResult(
            name="migrations",
            ok=False,
            detail=f"registry migration failed ({exc}) — run `nova db upgrade` manually",
        )
    return RestoreStepResult(
        name="migrations",
        ok=True,
        detail=f"registry schema at head (schema_version={revision})",
    )


def _replay_crypto_shreds(home: Path, decision_log_path: Path) -> RestoreStepResult:
    """Re-apply every applied CRYPTO_SHRED from the retention decision log (D4).

    For each applied shred record, the corresponding DEK must be absent from
    the restored DEK store — and is deleted if anything resurrected it. A
    record whose data subject cannot be resolved (no erasure receipt) fails
    the step: shred preservation cannot be proven for it.
    """
    name = "crypto-shred-replay"
    records = _read_shred_records(decision_log_path)
    if records is None:
        return RestoreStepResult(
            name=name,
            ok=True,
            detail=f"no retention decision log at {decision_log_path} — nothing to replay",
        )
    if not records:
        return RestoreStepResult(
            name=name, ok=True, detail="decision log has no applied crypto-shred records"
        )

    dek_db = home / "dek.db"
    if not dek_db.exists():
        return RestoreStepResult(
            name=name,
            ok=True,
            detail=(
                f"{len(records)} applied crypto-shred record(s); "
                "DEK store checked: none — nothing to re-shred"
            ),
        )

    absent = 0
    re_shredded = 0
    noop = 0
    unresolved: list[str] = []
    for record in records:
        if record.get("reason") == "no_dek":
            noop += 1  # nothing was ever shredded for this record
            continue
        subject_id = _resolve_subject(record)
        if subject_id is None:
            unresolved.append(str(record.get("record_id") or record.get("item_id")))
            continue
        if _dek_exists(dek_db, subject_id):
            _force_shred_dek(dek_db, subject_id)
            re_shredded += 1
        else:
            absent += 1

    detail = (
        f"{len(records)} applied crypto-shred record(s): {absent} already absent, "
        f"{re_shredded} resurrected DEK(s) re-destroyed, {noop} no-DEK no-op(s)"
    )
    if unresolved:
        return RestoreStepResult(
            name=name,
            ok=False,
            detail=(
                f"{detail}; {len(unresolved)} record(s) unresolvable "
                f"(no readable erasure receipt: {', '.join(unresolved)}) — "
                "shred preservation cannot be proven, restore failed"
            ),
        )
    return RestoreStepResult(name=name, ok=True, detail=detail)


def _verify_storage(home: Path) -> RestoreStepResult:
    """Doctor storage check (programmatic): backend reachable, schema present."""
    from novafabric.storage import get_backend

    registry_db = home / "registry.db"
    try:
        info = get_backend(backend="sqlite", db_path=registry_db).info()
    except Exception as exc:  # noqa: BLE001 — any failure is a failed restore
        return RestoreStepResult(
            name="verify-storage", ok=False, detail=f"storage check failed: {exc}"
        )
    if info.schema_version is None:
        return RestoreStepResult(
            name="verify-storage",
            ok=False,
            detail=f"restored registry at {registry_db} has no schema_version",
        )
    return RestoreStepResult(
        name="verify-storage",
        ok=True,
        detail=(
            f"backend={info.backend} schema_version={info.schema_version} "
            f"tables={len(info.row_counts)}"
        ),
    )


def _verify_seal_log(home: Path) -> RestoreStepResult:
    """`nova seal log verify` equivalent, when a Merkle log exists under home."""
    merkle_db = home / "novaseal-merkle.db"
    if not merkle_db.exists():
        return RestoreStepResult(
            name="verify-seal-log",
            ok=True,
            detail="no Merkle log under home — seal log verify skipped",
        )
    from novafabric.trust.novaseal.merkle import open_merkle_log

    try:
        result = open_merkle_log(merkle_db).verify_consistency()
    except Exception as exc:  # noqa: BLE001 — any failure is a failed restore
        return RestoreStepResult(
            name="verify-seal-log", ok=False, detail=f"seal log verify failed: {exc}"
        )
    if not result.consistent:
        return RestoreStepResult(
            name="verify-seal-log",
            ok=False,
            detail=(
                f"Merkle log INCONSISTENT ({len(result.errors)} error(s)): "
                + "; ".join(result.errors[:3])
            ),
        )
    return RestoreStepResult(
        name="verify-seal-log",
        ok=True,
        detail=f"Merkle log consistent ({result.leaf_count} leaves)",
    )


# ---------------------------------------------------------------------------
# Crypto-shred internals
# ---------------------------------------------------------------------------

def _read_shred_records(decision_log_path: Path) -> Optional[list[dict[str, object]]]:
    """Applied CRYPTO_SHRED RetentionActionRecords from the decision log.

    Returns None when no log exists; otherwise the (possibly empty) list.
    """
    if not decision_log_path.is_file():
        return None
    records: list[dict[str, object]] = []
    with decision_log_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # chain integrity is the audit verifier's job
            if entry.get("event_type") != _RETENTION_EVENT:
                continue
            details = entry.get("details") or {}
            if (
                details.get("action") == "crypto-shred"
                and details.get("outcome") == "applied"
            ):
                records.append(details)
    return records


def _resolve_subject(record: dict[str, object]) -> Optional[str]:
    """Data-subject id for a shred record, via its erasure receipt."""
    receipt_ref = record.get("erasure_receipt_ref")
    if not isinstance(receipt_ref, str) or not receipt_ref:
        return None
    receipt_path = Path(receipt_ref)
    if not receipt_path.is_file():
        return None
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    subject = receipt.get("subject_id")
    return subject if isinstance(subject, str) and subject else None


def _dek_exists(dek_db: Path, subject_id: str) -> bool:
    conn = sqlite3.connect(dek_db)
    try:
        row = conn.execute(
            "SELECT 1 FROM data_subject_deks WHERE subject_id = ?", (subject_id,)
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _force_shred_dek(dek_db: Path, subject_id: str) -> None:
    """Deterministically re-destroy a resurrected DEK (overwrite, then delete).

    This is the replay of an ALREADY-EVIDENCED shred (the decision log says it
    happened), so no retention-window deferral applies — the original erasure
    already cleared it. Mirrors ``DEKStore.erase_subject`` destruction
    semantics: overwrite the key bytes, then delete the row.
    """
    conn = sqlite3.connect(dek_db)
    try:
        with conn:
            conn.execute(
                "UPDATE data_subject_deks SET dek_hex = ? WHERE subject_id = ?",
                (os.urandom(32).hex(), subject_id),
            )
            conn.execute(
                "DELETE FROM data_subject_deks WHERE subject_id = ?", (subject_id,)
            )
    finally:
        conn.close()
