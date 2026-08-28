"""Restore a local-profile backup set (ADR-0181 second slice, experimental).

``restore_backup`` implements the spec's normative restore order
(``the private design/spec/backup-restore-v0.md`` §Restore procedure) for the LOCAL
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

``pg-dump`` sets are restored automatically since ADR-0217: pre-flight refuses
a non-empty target DB without ``--force`` (and takes a safety dump with it),
``pg_restore --clean --if-exists --single-transaction --no-owner
--no-privileges`` makes failure atomic, then ``alembic upgrade head``,
manifest-anchored row-count verification, and RLS re-application + proof run
as recorded steps. The DSN is never logged, never stored, and scrubbed from
every surfaced error.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from novafabric.object_capsule_store.worm.base import WormAdapter

from novafabric.backup.coverage import (
    default_audit_log_path,
    resolve_origin_root,
    strip_external_prefix,
)
from novafabric.backup.create import (
    PG_DUMP_MEMBER as _PG_DUMP_MEMBER,
)
from novafabric.backup.create import (
    _dsn_from_env,
    _redact_dsn,
    _run_pg_dump,
    is_denied,
)
from novafabric.backup.models import (
    PROFILE_LOCAL_FULL,
    PROFILE_MANIFEST_ONLY,
    PROFILE_PG_DUMP,
    BackupManifest,
    BackupMember,
    RestoreResult,
    RestoreStepResult,
    VerifyResult,
)
from novafabric.backup.verify import BackupVerifyError, read_manifest, verify_backup

#: Where displaced pre-existing data goes under the home when ``force`` is used.
_PRE_RESTORE_PREFIX = ".pre-restore-"

#: Retention decision-log event type carrying RetentionActionRecord payloads.
_RETENTION_EVENT = "retention.action"


class RestoreError(Exception):
    """Raised when a restore cannot proceed (the target home is left untouched
    for pre-extraction failures)."""


class PgRestoreNotFoundError(RestoreError):
    """``pg_restore`` is not on PATH — pg restore needs the Postgres client tools."""


def _failed_result(
    verdict: VerifyResult,
    home: Path,
    steps: list[RestoreStepResult],
    moved_aside: Optional[Path],
) -> RestoreResult:
    return RestoreResult(
        ok=False,
        set_id=verdict.set_id,
        profile=verdict.profile,
        home=str(home),
        steps=steps,
        moved_aside=str(moved_aside) if moved_aside else None,
    )


def restore_backup(
    set_path: Path,
    *,
    home: Path,
    force: bool = False,
    decision_log_path: Optional[Path] = None,
    restore_keys: bool = False,
    audit_log_path: Optional[Path] = None,
    keyring_dir: Optional[Path] = None,
    seal_config_dir: Optional[Path] = None,
    dsn: Optional[str] = None,
    adapter: Optional[object] = None,
    backend: Optional[str] = None,
    sample: int = 0,
) -> RestoreResult:
    """Restore the local-profile backup set at *set_path* into *home*.

    Args:
        set_path: Backup-set archive (``.tar.gz``) produced by ``create_backup``.
        home: Target NovaFabric home directory.
        force: Allow restoring into a non-empty home (or over existing
            external-origin files such as the audit log). Displaced data is
            moved into a timestamped ``.pre-restore-…/`` directory, never
            deleted.
        decision_log_path: Retention decision log (hash-chained audit JSONL)
            to replay crypto-shreds from. Defaults to the deployment's audit
            log path — which, when the set carried the audit log, is exactly
            where it was just restored.
        restore_keys: ADR-0216 D4 — restore ``key_material`` members. Default
            OFF: key members in the set are skipped (with a step detail), so
            key restoration requires an explicit opt-in on BOTH sides.
        audit_log_path: Audit-log destination override (tests / relocated
            deployments). Defaults to the deployment's audit log path.
        keyring_dir: Keyring destination override (tests).
        seal_config_dir: ``novaseal.yaml``/PEM destination override; defaults
            to the target home.

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

    # ADR-0217 D1: the VERIFIED manifest's profile drives dispatch — there is
    # no restore-side --profile flag.
    if verdict.profile == PROFILE_MANIFEST_ONLY:
        from novafabric.backup.restore_manifest import restore_manifest_backup

        resolved_adapter = _resolve_object_store_adapter(adapter, backend)
        return restore_manifest_backup(
            set_path,
            home=home,
            adapter=resolved_adapter,
            force=force,
            decision_log_path=decision_log_path,
            audit_log_path=audit_log_path,
            sample=sample,
        )
    is_pg = verdict.profile == PROFILE_PG_DUMP
    if verdict.profile not in (PROFILE_LOCAL_FULL, PROFILE_PG_DUMP):
        raise RestoreError(
            f"Set {verdict.set_id} has profile {verdict.profile!r} — only the "
            f"{PROFILE_LOCAL_FULL!r}, {PROFILE_PG_DUMP!r}, and "
            f"{PROFILE_MANIFEST_ONLY!r} profiles are restorable."
        )

    manifest = read_manifest(set_path)

    # Pg pre-flight runs BEFORE anything is touched (ADR-0217 D4).
    resolved_dsn: Optional[str] = None
    target_tables = 0
    if is_pg:
        resolved_dsn = _resolve_restore_dsn(dsn)
        _require_psycopg()
        target_tables = _check_target_db(resolved_dsn, force=force)
        steps.append(
            RestoreStepResult(
                name="check-target-db",
                ok=True,
                detail=(
                    f"target {_redact_dsn(resolved_dsn)}: {target_tables} "
                    "pre-existing table(s)"
                    + (" — safety dump will be taken" if target_tables else "")
                ),
            )
        )
    for member in manifest.members:
        _reject_unsafe_member(member.path, allow_keys=manifest.includes_keys)

    roots = {
        "audit_log_path": audit_log_path,
        "keyring_dir": keyring_dir,
        "seal_config_dir": seal_config_dir if seal_config_dir is not None else home,
    }
    restorable = [
        m for m in manifest.members
        if m.kind != "key_material" or restore_keys
    ]
    skipped_keys = len(manifest.members) - len(restorable)
    # The pg dump member is restored into the DATABASE, never into the home.
    pg_dump_member = next(
        (m for m in restorable if is_pg and m.path == _PG_DUMP_MEMBER), None
    )
    restorable = [m for m in restorable if m is not pg_dump_member]

    # --- 2. prepare the target home (and external destinations) -------------
    home_paths = [m.path for m in restorable if m.origin == "home"]
    moved_aside = _prepare_home(home, home_paths, force=force)
    moved_aside = _prepare_external(
        home, restorable, roots, force=force, pre_restore=moved_aside
    )
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

    # ADR-0217 D4: the database analogue of move-aside is a safety dump.
    if is_pg and target_tables > 0:
        if moved_aside is None:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            moved_aside = home / f"{_PRE_RESTORE_PREFIX}{timestamp}"
        steps.append(_safety_dump_db(resolved_dsn or "", moved_aside))
        if not steps[-1].ok:
            return _failed_result(verdict, home, steps, moved_aside)

    # --- 3. extract members (manifest-listed paths only) --------------------
    extracted = _extract_members(set_path, home, restorable, roots)
    detail = f"{extracted} member(s) extracted"
    if skipped_keys:
        detail += (
            f"; {skipped_keys} key member(s) SKIPPED — pass --restore-keys to "
            "restore key material"
        )
    steps.append(RestoreStepResult(name="extract", ok=True, detail=detail))

    # --- 3b. pg profile: restore the dump into the target DB (ADR-0217) -----
    if is_pg:
        if pg_dump_member is None:
            steps.append(
                RestoreStepResult(
                    name="pg-restore",
                    ok=False,
                    detail=f"pg-dump set has no {_PG_DUMP_MEMBER} member",
                )
            )
            return _failed_result(verdict, home, steps, moved_aside)
        steps.append(_run_pg_restore(set_path, resolved_dsn or "", manifest))
        if not steps[-1].ok:
            return _failed_result(verdict, home, steps, moved_aside)
        steps.append(_run_pg_migrations(resolved_dsn or ""))

    # --- 4. migrations to head ----------------------------------------------
    steps.append(_run_migrations(home))

    # --- 5. NORMATIVE crypto-shred replay (ADR-0181 D4) ----------------------
    # Default decision log = the deployment audit-log path; when the set
    # carried the audit log it was just restored there, so a fresh-machine DR
    # restore replays the shreds it brought along. CRUCIALLY, when a LIVE
    # audit log was moved aside by --force, it is a superset of the restored
    # (older) one — shreds applied AFTER the backup was taken live only there.
    # Both logs are replayed; replay is idempotent, so the union is safe and
    # the only correct choice (D4: shredded stays shredded).
    decision_logs: list[Path] = []
    if decision_log_path is not None:
        decision_logs.append(decision_log_path)
    else:
        decision_logs.append(audit_log_path or default_audit_log_path())
    if moved_aside is not None:
        aside_audit = moved_aside / "external" / "audit" / "audit.jsonl"
        if aside_audit.is_file():
            decision_logs.append(aside_audit)
    steps.append(_replay_crypto_shreds(home, decision_logs))

    # --- 6. ratchet epoch-regression handling (ADR-0216 D5) -----------------
    steps.append(_ratchet_advance(home))

    # --- 7. verification chain (restore complete ONLY when this passes) -----
    if is_pg:
        steps.append(_verify_pg_counts(resolved_dsn or "", manifest))
        steps.append(_verify_pg_rls(resolved_dsn or ""))
    steps.append(_verify_storage(home))
    steps.append(_verify_seal_log(home))
    steps.append(_verify_state_dbs(home, restorable))

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


def _reject_unsafe_member(path: str, *, allow_keys: bool = False) -> None:
    pure = PurePosixPath(path)
    if pure.is_absolute() or path.startswith(("/", "\\", "~")) or (
        len(path) > 1 and path[1] == ":"
    ):
        raise RestoreError(f"Unsafe member path in set (absolute): {path!r}")
    if ".." in pure.parts:
        raise RestoreError(f"Unsafe member path in set (traversal): {path!r}")
    if is_denied(pure, allow_keys=allow_keys):
        raise RestoreError(
            f"Set lists excluded path {path!r} — key material must never be "
            "restored from a backup set"
        )


def _dest_for(
    member: BackupMember, home: Path, roots: dict[str, Optional[Path]]
) -> Path:
    """Destination path for a member, honoring its origin (ADR-0216 D1)."""
    root = resolve_origin_root(
        member.origin,
        home,
        audit_log_path=roots.get("audit_log_path"),
        keyring_dir=roots.get("keyring_dir"),
        seal_config_dir=roots.get("seal_config_dir"),
    )
    return root / strip_external_prefix(member.path)


def _prepare_external(
    home: Path,
    members: list[BackupMember],
    roots: dict[str, Optional[Path]],
    *,
    force: bool,
    pre_restore: Optional[Path],
) -> Optional[Path]:
    """Never silently overwrite external-origin files (audit log above all).

    An existing destination is refused without *force*; with it, the file is
    moved into the same timestamped ``.pre-restore-…/`` directory as displaced
    home data, under ``external/<origin>/``.
    """
    external = [m for m in members if m.origin != "home"]
    if not external:
        return pre_restore
    for member in external:
        dst = _dest_for(member, home, roots)
        if not dst.exists():
            continue
        if not force:
            raise RestoreError(
                f"Restore target {dst} already exists (member {member.path!r}) — "
                "refusing to overwrite live external state (it may be the "
                "hash-chained audit log). Pass --force to move it aside."
            )
        if pre_restore is None:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            pre_restore = home / f"{_PRE_RESTORE_PREFIX}{timestamp}"
        aside = pre_restore / "external" / member.origin / strip_external_prefix(member.path)
        aside.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dst), str(aside))
    return pre_restore


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


def _extract_members(
    set_path: Path,
    home: Path,
    members: list[BackupMember],
    roots: dict[str, Optional[Path]],
) -> int:
    """Extract exactly the manifest-listed members, safely, as regular files.

    Home members land under *home* (0.1.x behaviour, byte-identical);
    external members land under their origin roots. Sensitive members are
    chmod'd 0600 after writing (ratchet dirs 0700) — tar metadata is never
    trusted for modes (ADR-0216 D3).
    """
    home_resolved = home.resolve()
    count = 0
    with tarfile.open(set_path, "r:gz") as tar:
        for member in members:
            fh = tar.extractfile(member.path)
            if fh is None:  # verify guarantees presence; belt-and-suspenders
                raise RestoreError(f"Member {member.path!r} vanished from the archive")
            dst = _dest_for(member, home, roots)
            if member.origin == "home" and not dst.resolve().is_relative_to(
                home_resolved
            ):
                raise RestoreError(f"Member {member.path!r} escapes the target home")
            dst.parent.mkdir(parents=True, exist_ok=True)
            with fh, open(dst, "wb") as out:
                shutil.copyfileobj(fh, out)
            if member.sensitive:
                os.chmod(dst, 0o600)
            count += 1
    ratchet_dir = home / "seal" / "ratchet"
    if ratchet_dir.is_dir():
        os.chmod(ratchet_dir, 0o700)
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


def _replay_crypto_shreds(
    home: Path, decision_log_paths: Path | list[Path]
) -> RestoreStepResult:
    """Re-apply every applied CRYPTO_SHRED from the retention decision log(s) (D4).

    For each applied shred record, the corresponding DEK must be absent from
    the restored DEK store — and is deleted if anything resurrected it. A
    record whose data subject cannot be resolved (no erasure receipt) fails
    the step: shred preservation cannot be proven for it. Multiple logs (the
    restored one plus a moved-aside live one) are merged; replay is
    idempotent, so the union is always safe.
    """
    name = "crypto-shred-replay"
    paths = (
        decision_log_paths
        if isinstance(decision_log_paths, list)
        else [decision_log_paths]
    )
    records: Optional[list[dict[str, object]]] = None
    for log_path in paths:
        found = _read_shred_records(log_path)
        if found is None:
            continue
        records = found if records is None else records + found
    if records is None:
        return RestoreStepResult(
            name=name,
            ok=True,
            detail=(
                "no retention decision log at "
                f"{', '.join(str(p) for p in paths)} — nothing to replay"
            ),
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


def _resolve_object_store_adapter(
    adapter: Optional[object], backend: Optional[str]
) -> "WormAdapter":
    """Adapter for a manifest-only restore: injected, or built from --backend."""
    from novafabric.object_capsule_store.worm.base import WormAdapter

    if adapter is not None:
        if not isinstance(adapter, WormAdapter):
            raise RestoreError("adapter must be a WormAdapter instance")
        return adapter
    resolved_backend = backend or os.environ.get("NOVA_OCS_BACKEND")
    if not resolved_backend:
        raise RestoreError(
            "Restoring a manifest-only set requires an object-store backend — "
            "pass --backend (or set NOVA_OCS_BACKEND)"
        )
    from novafabric.object_capsule_store.backend_router import make_adapter

    try:
        return make_adapter(backend=resolved_backend)
    except Exception as exc:  # noqa: BLE001 — surface a legible restore error
        raise RestoreError(
            f"Cannot initialise object-store backend {resolved_backend!r}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Pg-profile steps (ADR-0217)
# ---------------------------------------------------------------------------

def _resolve_restore_dsn(dsn: Optional[str]) -> str:
    resolved = dsn or _dsn_from_env()
    if resolved is None:
        raise RestoreError(
            "Restoring a pg-dump set requires a DSN — pass --dsn or set "
            "NOVA_DSN (or NOVAFABRIC_POSTGRES_DSN). The DSN is never logged."
        )
    return resolved


def _require_psycopg() -> None:
    try:
        import psycopg  # noqa: F401
    except ImportError as exc:
        raise RestoreError(
            "Restoring a pg-dump set requires psycopg — install the server "
            "extra: pip install 'novafabric[server]'"
        ) from exc


def _check_target_db(dsn: str, *, force: bool) -> int:
    """Pre-flight (ADR-0217 D4): refuse a non-empty target DB without force.

    Returns the number of pre-existing tables in schema ``public``. Runs
    before ANYTHING is touched; failure leaves home and DB unmodified.
    """
    import psycopg

    try:
        with psycopg.connect(dsn) as conn:
            row = conn.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            ).fetchone()
    except psycopg.Error as exc:
        raise RestoreError(
            "Cannot reach the target database "
            f"({_redact_dsn(dsn)}): {str(exc).replace(dsn, '<dsn redacted>')}"
        ) from exc
    count = int(row[0]) if row else 0
    if count and not force:
        raise RestoreError(
            f"Target database {_redact_dsn(dsn)} is not empty ({count} "
            "table(s) in schema public) — refusing to overwrite. Pass --force "
            "to proceed; a pre-restore safety dump is taken first and pg_restore "
            "runs in a single transaction (rollback leaves the DB unchanged)."
        )
    return count


def _safety_dump_db(dsn: str, pre_restore: Path) -> RestoreStepResult:
    """Dump-before-restore: the DB analogue of the home move-aside (D4)."""
    name = "safety-dump"
    pre_restore.mkdir(parents=True, exist_ok=True)
    dump_path = pre_restore / "db.pre-restore.pgdump"
    try:
        _run_pg_dump(dsn, dump_path)
    except Exception as exc:  # noqa: BLE001 — a failed safety dump fails the restore
        return RestoreStepResult(
            name=name,
            ok=False,
            detail=f"pre-restore safety dump failed: {exc} — restore aborted",
        )
    return RestoreStepResult(
        name=name, ok=True, detail=f"pre-restore state dumped to {dump_path}"
    )


def _run_pg_restore(
    set_path: Path, dsn: str, manifest: BackupManifest
) -> RestoreStepResult:
    """``pg_restore --clean --if-exists --single-transaction`` (ADR-0217 D3).

    ``--single-transaction`` makes failure atomic (target unchanged);
    ``--no-owner --no-privileges`` tolerates role skew — RLS is re-verified
    programmatically afterwards rather than trusted from the dump.
    """
    import shutil as _shutil
    import subprocess
    import tempfile

    name = "pg-restore"
    pg_restore = _shutil.which("pg_restore")
    if pg_restore is None:
        raise PgRestoreNotFoundError(
            "pg_restore not found on PATH — restoring a pg-dump set needs the "
            "Postgres client tools (Debian/Ubuntu: `apt install "
            "postgresql-client`; macOS: `brew install libpq`)"
        )

    skew = ""
    if manifest.pg_client_version:
        proc = subprocess.run(  # noqa: S603
            [pg_restore, "--version"], capture_output=True, text=True
        )
        local_version = proc.stdout.strip()
        if local_version and local_version.split()[-1].split(".")[0] != (
            manifest.pg_client_version.split()[-1].split(".")[0]
        ):
            skew = (
                f" [WARNING: version skew — dump made with "
                f"{manifest.pg_client_version!r}, restoring with "
                f"{local_version!r}]"
            )

    with tempfile.TemporaryDirectory(prefix="nova-restore-pg-") as tmp:
        dump_path = Path(tmp) / _PG_DUMP_MEMBER
        with tarfile.open(set_path, "r:gz") as tar:
            fh = tar.extractfile(_PG_DUMP_MEMBER)
            if fh is None:
                return RestoreStepResult(
                    name=name, ok=False, detail=f"{_PG_DUMP_MEMBER} vanished from set"
                )
            with fh, open(dump_path, "wb") as out:
                shutil.copyfileobj(fh, out)
        proc = subprocess.run(  # noqa: S603 — fixed binary, no shell
            [
                pg_restore,
                "--clean",
                "--if-exists",
                "--single-transaction",
                "--no-owner",
                "--no-privileges",
                "--dbname",
                dsn,
                str(dump_path),
            ],
            capture_output=True,
            text=True,
        )
    if proc.returncode != 0:
        detail = (proc.stderr or "no stderr").replace(dsn, "<dsn redacted>").strip()
        return RestoreStepResult(
            name=name,
            ok=False,
            detail=(
                f"pg_restore exited {proc.returncode} (single transaction rolled "
                f"back — target unchanged): {detail[:500]}{skew}"
            ),
        )
    return RestoreStepResult(
        name=name,
        ok=True,
        detail=f"dump restored into {_redact_dsn(dsn)} (single transaction){skew}",
    )


def _run_pg_migrations(dsn: str) -> RestoreStepResult:
    """``alembic upgrade head`` to bring an older dump to schema head."""
    from novafabric.metadata_store.cli import run_alembic_upgrade

    ok, detail = run_alembic_upgrade("postgres", dsn=dsn)
    return RestoreStepResult(name="pg-migrations", ok=ok, detail=detail)


def _verify_pg_counts(dsn: str, manifest: BackupManifest) -> RestoreStepResult:
    """Live per-table counts vs dump-time counts recorded in the manifest.

    Honest slice (ADR-0217 D5): sets that predate manifest 0.2.0 (or were
    dumped without psycopg) carry no counts — the step passes with an explicit
    "skipped" detail rather than pretending to have verified anything.
    """
    import psycopg

    name = "verify-db-counts"
    if not manifest.pg_table_counts:
        return RestoreStepResult(
            name=name,
            ok=True,
            detail="set predates recorded row counts — count verification skipped",
        )
    mismatches: list[str] = []
    checked = 0
    try:
        with psycopg.connect(dsn) as conn:
            for table, expected in manifest.pg_table_counts.items():
                row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
                actual = int(row[0]) if row else 0
                checked += 1
                if actual != expected:
                    mismatches.append(f"{table}: expected {expected}, got {actual}")
    except psycopg.Error as exc:
        return RestoreStepResult(
            name=name,
            ok=False,
            detail=f"count verification failed: {str(exc).replace(dsn, '<dsn redacted>')}",
        )
    if mismatches:
        return RestoreStepResult(name=name, ok=False, detail="; ".join(mismatches))
    return RestoreStepResult(
        name=name, ok=True, detail=f"{checked} table count(s) match the manifest"
    )


def _verify_pg_rls(dsn: str) -> RestoreStepResult:
    """Re-apply and verify RLS posture (ADR-0217 D5.3).

    ``--no-privileges`` restores may lose role-linked state; the idempotent
    schema-ensure re-applies ENABLE/FORCE RLS + the tenant_isolation policy,
    then the rls helpers assert it — proven, not assumed.

    All three verifiers ADR-0229 names are run, and the role split is not
    optional among them. ``BYPASSRLS`` on a role makes Postgres skip row-level
    security outright, whatever the table's forced-RLS flag says and whatever
    the policy text is, so a restore into a cluster where ``novafabric_app``
    carries it would satisfy both table-level checks while tenant isolation is
    entirely defeated. Checking only those two is not a weaker proof of the same
    thing — it is a proof that goes vacuous under exactly the condition the
    third check exists to detect. And ``--no-privileges`` losing *role-linked*
    state, this function's own stated reason for re-verifying, is precisely how
    that attribute drifts.
    """
    import psycopg

    from novafabric.metadata_store.postgres import _TENANT_TABLES, PostgresMetadataStore
    from novafabric.metadata_store.rls import (
        verify_force_rls,
        verify_policy_text,
        verify_role_split,
    )

    name = "verify-rls"
    try:
        store = PostgresMetadataStore(dsn)
        store.bootstrap()
        with psycopg.connect(dsn) as conn:
            forced = verify_force_rls(conn, _TENANT_TABLES)
            policies = verify_policy_text(conn, _TENANT_TABLES)
            roles = verify_role_split(conn)
    except Exception as exc:  # noqa: BLE001 — any failure is a failed restore
        return RestoreStepResult(
            name=name,
            ok=False,
            detail=f"RLS verification failed: {str(exc).replace(dsn, '<dsn redacted>')}",
        )
    bad = [t for t in _TENANT_TABLES if not forced.get(t) or not policies.get(t)]
    if bad:
        return RestoreStepResult(
            name=name,
            ok=False,
            detail=f"RLS not enforced on: {', '.join(bad)}",
        )
    # A missing key is a failure, not a pass: verify_role_split() omits any role
    # it did not find, so treating absence as "no BYPASSRLS" would report success
    # precisely when the app role does not exist to be checked.
    if roles.get("novafabric_app_bypassrls") is not False:
        return RestoreStepResult(
            name=name,
            ok=False,
            detail=(
                "novafabric_app has BYPASSRLS or is missing — RLS is bypassed "
                "regardless of FORCE RLS and policy text; tenant isolation is not enforced"
            ),
        )
    return RestoreStepResult(
        name=name,
        ok=True,
        detail=(
            f"FORCE RLS + tenant_isolation verified on {len(_TENANT_TABLES)} table(s); "
            "novafabric_app confirmed without BYPASSRLS"
        ),
    )


def _ratchet_advance(home: Path) -> RestoreStepResult:
    """Burn regressed ratchet epochs after restore (ADR-0216 D5).

    A restored ratchet state whose epoch is behind the restored epoch
    registry's maximum could re-derive already-rotated epoch keys — a
    forward-security violation. Rotate each such node past the registry max.
    Undetectable when the registry itself was lost (residual risk, documented:
    operators SHOULD rotate once after any restore).
    """
    name = "ratchet-advance"
    state_dir = home / "seal" / "ratchet"
    registry = state_dir / "epoch-registry.jsonl"
    if not state_dir.is_dir() or not registry.is_file():
        return RestoreStepResult(
            name=name, ok=True, detail="no ratchet state in restored home — skipped"
        )

    max_epoch: dict[str, int] = {}
    with registry.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            node = record.get("node_id")
            epoch = record.get("epoch")
            if isinstance(node, str) and isinstance(epoch, int):
                max_epoch[node] = max(max_epoch.get(node, -1), epoch)

    from novafabric.trust.novaseal.ratchet import RatchetError, load_state, rotate

    advanced: list[str] = []
    current: list[str] = []
    for state_file in sorted(state_dir.glob("*.json")):
        node_id = state_file.stem
        try:
            state = load_state(node_id, state_dir)
        except RatchetError as exc:
            return RestoreStepResult(
                name=name, ok=False, detail=f"corrupt ratchet state for {node_id!r}: {exc}"
            )
        registered = max_epoch.get(node_id, state.epoch)
        if state.epoch >= registered:
            current.append(f"{node_id}@{state.epoch}")
            continue
        start = state.epoch
        try:
            while state.epoch <= registered:
                state = rotate(node_id, state_dir)
        except RatchetError as exc:
            return RestoreStepResult(
                name=name,
                ok=False,
                detail=f"failed to advance {node_id!r} past regressed epochs: {exc}",
            )
        advanced.append(
            f"{node_id} advanced {start}→{state.epoch} (epoch regression on restore)"
        )

    detail_parts = []
    if advanced:
        detail_parts.append("; ".join(advanced))
    if current:
        detail_parts.append(f"{len(current)} node(s) already current")
    return RestoreStepResult(
        name=name, ok=True, detail="; ".join(detail_parts) or "no ratchet nodes"
    )


def _verify_state_dbs(home: Path, members: list[BackupMember]) -> RestoreStepResult:
    """Every restored store must be provably openable (ADR-0216 D5).

    SQLite members pass ``PRAGMA integrity_check``; DuckDB opens read-only.
    Restore is not complete until every store opens clean.
    """
    name = "verify-state-dbs"
    failures: list[str] = []
    checked = 0
    for member in members:
        if member.origin != "home" or not member.path.endswith((".db", ".duckdb")):
            continue
        db_path = home / member.path
        if not db_path.is_file():
            failures.append(f"{member.path}: missing after extraction")
            continue
        if member.path.endswith(".duckdb"):
            checked += 1
            try:
                import duckdb

                duckdb.connect(str(db_path), read_only=True).close()
            except ImportError:
                checked -= 1  # honestly not checked rather than silently passed
                failures.append(
                    f"{member.path}: duckdb module unavailable — cannot verify"
                )
            except Exception as exc:  # noqa: BLE001 — any failure fails the restore
                failures.append(f"{member.path}: {exc}")
            continue
        checked += 1
        try:
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute("PRAGMA integrity_check").fetchone()
            finally:
                conn.close()
            if row is None or row[0] != "ok":
                failures.append(f"{member.path}: integrity_check={row[0] if row else None}")
        except sqlite3.Error as exc:
            failures.append(f"{member.path}: {exc}")
    if failures:
        return RestoreStepResult(
            name=name, ok=False, detail="; ".join(failures)
        )
    return RestoreStepResult(
        name=name, ok=True, detail=f"{checked} state store(s) open clean"
    )


def _verify_storage(home: Path) -> RestoreStepResult:
    """Doctor storage check (programmatic): backend reachable, schema present."""
    from novafabric.storage import get_backend

    registry_db = home / "registry.db"
    if not registry_db.exists():
        # Only possible for pg-dump sets (the local profile requires
        # registry.db at create time): the metadata lives in Postgres and was
        # verified by the pg steps.
        return RestoreStepResult(
            name="verify-storage",
            ok=True,
            detail="no registry.db in set — storage check delegated to pg steps",
        )
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
