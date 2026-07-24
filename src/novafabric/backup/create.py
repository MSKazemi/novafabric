"""Create an evidence-grade local backup set (ADR-0181 first slice).

``create_backup`` produces a ``.tar.gz`` backup set of a LOCAL (SQLite)
NovaFabric deployment:

- ``registry.db`` — snapshotted via the SQLite **online-backup API**, which is
  safe against a live writer (never a live-file copy, per ADR-0181 D5);
- the capsule directories (``capsules/`` and/or ``runs/`` under the home);
- a **secret-redacted** copy of ``config.yaml`` when present.

NORMATIVE exclusions (ADR-0181 D3 / spec "Contents and exclusions") are
enforced by a deny-filter, not convention: nothing under a ``keys/`` path, no
``*.pem`` / ``*.key`` files, no ``.serve-token`` / ``.server-token``, no env
files. Key material never travels in a backup set.

The manifest is DSSE-signed when a **local** NovaSeal signing profile is
configured; otherwise the set carries an honest ``signature: null`` +
``signing_status: "unsigned"`` (worm-conformance honesty pattern).

Second slice (ADR-0181): ``profile="pg"`` adds a ``pg_dump --format=custom``
member for a server (Postgres) deployment. The DSN is read from ``--dsn`` /
``NOVA_DSN`` / ``NOVAFABRIC_POSTGRES_DSN`` and is NEVER logged, never raised in
an error message, and never stored — the manifest records only the redacted
``host/dbname`` as ``db_target``. Restore of a pg-dump set is NOT implemented
in this slice: use the ``pg_restore`` runbook (``docs/ops/backup-restore.md``
§1.2).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlsplit

from novafabric import __version__ as _nova_version
from novafabric.backup.coverage import (
    CoverageError,
    collect_extended_components,
    sqlite_snapshot,
)
from novafabric.backup.models import (
    PROFILE_LOCAL_FULL,
    PROFILE_MANIFEST_ONLY,
    PROFILE_PG_DUMP,
    BackupManifest,
    BackupMember,
    CoverageEntry,
    ManifestSignature,
)

if TYPE_CHECKING:  # pragma: no cover
    from novafabric.object_capsule_store.worm.base import WormAdapter
from novafabric.capture._ulid import new_ulid
from novafabric.capture.secrets import redact_secrets_in_text
from novafabric.trust.novaseal.config import SealConfigError, load_signing_profile
from novafabric.trust.novaseal.envelope import EnvelopeError, create_envelope

#: In-archive name of the manifest (not itself a member — it is the trust root).
MANIFEST_NAME = "manifest.json"

#: In-archive name of the DSSE envelope over the manifest body, when signed.
MANIFEST_DSSE_NAME = "manifest.dsse.json"

#: Capsule directories backed up from the NovaFabric home, when present.
_CAPSULE_DIRS = ("capsules", "runs")

_DENY_SUFFIXES = (".pem", ".key")
_DENY_NAMES = frozenset({".serve-token", ".server-token", ".env"})
_DENY_DIR_COMPONENTS = frozenset({"keys"})

_UNSIGNED_DETAIL = (
    "No local NovaSeal signing profile is configured; this manifest carries "
    "per-member SHA-256 integrity digests but is NOT cryptographically signed."
)


#: In-archive name of the Postgres dump member (pg profile).
PG_DUMP_MEMBER = "db.pgdump"

#: Env vars consulted (in order) when ``--dsn`` is not given for the pg profile.
_DSN_ENV_VARS = ("NOVA_DSN", "NOVAFABRIC_POSTGRES_DSN")


class BackupCreateError(Exception):
    """Raised when a backup set cannot be created."""


class PgDumpNotFoundError(BackupCreateError):
    """``pg_dump`` is not on PATH — the pg profile needs the Postgres client tools."""


@dataclass(frozen=True)
class BackupCreateResult:
    """Outcome of :func:`create_backup`."""

    archive_path: Path
    manifest: BackupManifest


def is_denied(rel_path: PurePosixPath | str, *, allow_keys: bool = False) -> bool:
    """True if *rel_path* must never enter a backup set (normative deny-filter).

    With ``allow_keys`` (ADR-0216 D4, ``--include-keys`` sets whose manifest
    says ``includes_keys: true``), ONLY paths under the reserved
    ``external/keyring/`` and ``external/seal-config/`` prefixes are permitted
    to carry key material; every other denial (``.env``, tokens,
    capsule-embedded ``keys/`` dirs) stays unconditional.
    """
    rel = PurePosixPath(rel_path)
    if allow_keys:
        parts = rel.parts
        if len(parts) >= 3 and parts[0] == "external" and parts[1] in (
            "keyring",
            "seal-config",
        ):
            return rel.name in _DENY_NAMES
    if any(part in _DENY_DIR_COMPONENTS for part in rel.parts):
        return True
    name = rel.name
    if name in _DENY_NAMES:
        return True
    return any(name.endswith(suffix) for suffix in _DENY_SUFFIXES)


def create_backup(
    output_path: Path,
    *,
    home: Path,
    profile: str = "local",
    dsn: Optional[str] = None,
    include_keys: bool = False,
    audit_log_path: Optional[Path] = None,
    keyring_dir: Optional[Path] = None,
    seal_config_path: Optional[Path] = None,
    adapter: Optional["WormAdapter"] = None,
    backend: Optional[str] = None,
    tenants: Optional[list[str]] = None,
    allow_pending_wal: bool = False,
    deep: bool = False,
    wal_db_path: Optional[Path] = None,
) -> BackupCreateResult:
    """Build a backup set of the deployment under *home*.

    Args:
        output_path: Target ``.tar.gz`` file, or an existing directory in which
            ``nova-backup-<set_id>.tar.gz`` is created.
        home: NovaFabric home directory (``~/.novafabric`` layout).
        profile: ``"local"`` (default, ``local-full`` set) or ``"pg"`` /
            ``"postgres"`` (adds a ``pg_dump --format=custom`` member).
        dsn: Postgres connection string for the pg profile (falls back to
            ``NOVA_DSN`` / ``NOVAFABRIC_POSTGRES_DSN``). Never logged and never
            stored — the manifest carries only the redacted ``host/dbname``.
        include_keys: ADR-0216 D4 — also pack the signing keyring and
            ``novaseal.yaml`` + its key/cert PEMs under ``external/…``. Default
            OFF: key material never travels in a set unless explicitly opted
            in on BOTH create (`--include-keys`) and restore (`--restore-keys`).
        audit_log_path: Hash-chained audit log to include (defaults to the
            deployment's audit log path; injectable for hermetic tests).
        keyring_dir: Signing-keyring directory override (tests).
        seal_config_path: ``novaseal.yaml`` path override (tests); default is
            ``<home>/novaseal.yaml``.

    Raises:
        BackupCreateError: when there is nothing to back up, the archive cannot
            be written, or (pg profile) the dump fails / no DSN is available.
        PgDumpNotFoundError: pg profile with no ``pg_dump`` binary on PATH.
    """
    if profile in ("pg", "postgres", PROFILE_PG_DUMP):
        manifest_profile = PROFILE_PG_DUMP
    elif profile in ("local", PROFILE_LOCAL_FULL):
        manifest_profile = PROFILE_LOCAL_FULL
    elif profile in ("manifest", PROFILE_MANIFEST_ONLY):
        manifest_profile = PROFILE_MANIFEST_ONLY
    else:
        raise BackupCreateError(
            f"Unknown backup profile {profile!r} — expected 'local', 'pg', "
            "or 'manifest'"
        )

    home = home.expanduser()
    registry_db = home / "registry.db"
    if manifest_profile == PROFILE_LOCAL_FULL and not registry_db.exists():
        raise BackupCreateError(f"No registry.db found under {home} — nothing to back up")

    set_id = new_ulid()
    created_at = datetime.now(timezone.utc).isoformat()

    if output_path.is_dir():
        archive_path = output_path / f"nova-backup-{set_id}.tar.gz"
    else:
        archive_path = output_path

    with tempfile.TemporaryDirectory(prefix="nova-backup-") as tmp:
        stage = Path(tmp)

        members: list[BackupMember] = []
        coverage: list[CoverageEntry] = []
        db_dump_ref: Optional[str] = None
        db_target: Optional[str] = None
        schema_revision: Optional[str] = None

        # --- pg profile: pg_dump custom-format member (DSN never logged) ---
        pg_schema_revision: Optional[str] = None
        pg_table_counts: Optional[dict[str, int]] = None
        pg_client_version: Optional[str] = None
        if manifest_profile == PROFILE_PG_DUMP:
            resolved_dsn = dsn or _dsn_from_env()
            if resolved_dsn is None:
                raise BackupCreateError(
                    "pg profile requires a DSN — pass --dsn or set NOVA_DSN "
                    "(or NOVAFABRIC_POSTGRES_DSN)"
                )
            dump_path = stage / PG_DUMP_MEMBER
            _run_pg_dump(resolved_dsn, dump_path)
            db_target = _redact_dsn(resolved_dsn)
            db_dump_ref = PG_DUMP_MEMBER
            members.append(_member(dump_path, PG_DUMP_MEMBER, kind="db_dump"))
            # ADR-0217 D5/D6: dump-time verification anchors. Best-effort —
            # psycopg may be absent (dump needs only the client tools); restore
            # then reports count verification as honestly skipped.
            pg_schema_revision, pg_table_counts = _pg_verification_fields(resolved_dsn)
            pg_client_version = _pg_client_version()

        # --- registry.db via the SQLite online-backup API (live-writer safe) ---
        # Required for the local profile; included in the pg profile when present.
        if registry_db.exists():
            snapshot = stage / "registry.db"
            _sqlite_snapshot(registry_db, snapshot)
            schema_revision = _read_schema_revision(snapshot)
            members.append(_member(snapshot, "registry.db", kind="registry", role="registry"))
            coverage.append(CoverageEntry(component="registry", status="included"))
            if db_dump_ref is None:
                db_dump_ref = "registry.db"
        else:
            coverage.append(
                CoverageEntry(component="registry", status="absent", detail="no registry.db")
            )

        # --- capsule directories ---
        # Manifest-only sets carry NO capsule blobs (ADR-0181 D2): the WORM
        # bucket is the durability layer, the listing below is the proof.
        capsule_members = 0
        capsule_dirs = _CAPSULE_DIRS if manifest_profile != PROFILE_MANIFEST_ONLY else ()
        for dirname in capsule_dirs:
            src_dir = home / dirname
            if not src_dir.is_dir():
                continue
            for src in sorted(p for p in src_dir.rglob("*") if p.is_file()):
                rel = PurePosixPath(dirname) / src.relative_to(src_dir).as_posix()
                if is_denied(rel):
                    continue
                dst = stage / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                members.append(_member(dst, str(rel), kind="blob", role="capsules"))
                capsule_members += 1
        if manifest_profile == PROFILE_MANIFEST_ONLY:
            coverage.append(
                CoverageEntry(
                    component="capsules",
                    status="skipped",
                    detail=(
                        "manifest-only profile: capsule blobs stay in the WORM "
                        "bucket; the signed listing pins every chain head"
                    ),
                )
            )
        else:
            coverage.append(
                CoverageEntry(component="capsules", status="included")
                if capsule_members
                else CoverageEntry(
                    component="capsules", status="absent", detail="no capsule directories"
                )
            )

        # --- redacted config ---
        redacted_config: Optional[str] = None
        config_src = home / "config.yaml"
        if config_src.is_file():
            redacted_config = "config.redacted.yaml"
            redacted_path = stage / redacted_config
            redacted_path.write_text(
                redact_secrets_in_text(config_src.read_text()), encoding="utf-8"
            )
            members.append(
                _member(redacted_path, redacted_config, kind="config", role="config")
            )
            coverage.append(CoverageEntry(component="config", status="included"))
        else:
            coverage.append(
                CoverageEntry(component="config", status="absent", detail="no config.yaml")
            )

        # --- ADR-0216 D1: every remaining persistent local store --------------
        # The manifest-only profile targets the object store, not local
        # stores — recorded as one honest skip row, not silence.
        if manifest_profile == PROFILE_MANIFEST_ONLY:
            coverage.append(
                CoverageEntry(
                    component="local-stores",
                    status="skipped",
                    detail=(
                        "manifest-only profile covers the object store; use the "
                        "local profile for local state stores"
                    ),
                )
            )
        else:
            for collected in collect_extended_components(
                home,
                stage,
                _member,
                audit_log_path=audit_log_path,
                include_keys=include_keys,
                keyring_dir=keyring_dir,
                seal_config_path=seal_config_path,
            ):
                members.extend(collected.members)
                coverage.append(collected.coverage)

        # --- ADR-0216 D6: manifest-only object-store listing -------------------
        object_store_manifest_ref: Optional[str] = None
        object_store_backend: Optional[str] = None
        object_store_fingerprint: Optional[str] = None
        if manifest_profile == PROFILE_MANIFEST_ONLY:
            listing_member, listing_coverage, listing_meta = _collect_object_store(
                stage,
                adapter=adapter,
                backend=backend,
                tenants=tenants,
                allow_pending_wal=allow_pending_wal,
                deep=deep,
                wal_db_path=wal_db_path,
            )
            members.append(listing_member)
            coverage.append(listing_coverage)
            object_store_manifest_ref, object_store_backend, object_store_fingerprint = (
                listing_meta
            )

        if not members:
            raise BackupCreateError(f"Nothing to back up under {home}")

        manifest = BackupManifest(
            set_id=set_id,
            created_at=created_at,
            profile=manifest_profile,
            nova_version=_nova_version,
            schema_revision=schema_revision,
            members=members,
            db_dump=db_dump_ref,
            object_store_manifest=object_store_manifest_ref,
            object_store_backend=object_store_backend,
            object_store_fingerprint=object_store_fingerprint,
            redacted_config=redacted_config,
            db_target=db_target,
            coverage=coverage,
            includes_keys=include_keys and any(m.kind == "key_material" for m in members),
            pg_schema_revision=pg_schema_revision,
            pg_table_counts=pg_table_counts,
            pg_client_version=pg_client_version,
        )
        manifest, dsse_bytes = _sign_manifest(manifest)

        manifest_path = stage / MANIFEST_NAME
        manifest_path.write_text(
            json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        dsse_path: Optional[Path] = None
        if dsse_bytes is not None:
            dsse_path = stage / MANIFEST_DSSE_NAME
            dsse_path.write_bytes(dsse_bytes)

        _write_archive(
            archive_path,
            manifest_path,
            dsse_path,
            members,
            stage,
            allow_keys=manifest.includes_keys,
        )

    return BackupCreateResult(archive_path=archive_path, manifest=manifest)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _sqlite_snapshot(src_db: Path, dst_db: Path) -> None:
    """Consistent SQLite snapshot via the online-backup API (ADR-0181 D5)."""
    try:
        sqlite_snapshot(src_db, dst_db)
    except CoverageError as exc:
        raise BackupCreateError(str(exc)) from exc


def _dsn_from_env() -> Optional[str]:
    for var in _DSN_ENV_VARS:
        value = os.environ.get(var)
        if value:
            return value
    return None


def _redact_dsn(dsn: str) -> str:
    """Reduce a DSN to ``host/dbname`` — never credentials, never the raw string.

    Handles URL DSNs (``postgresql://user:pass@host:5432/db``) and keyword DSNs
    (``host=… dbname=…``). Anything unparseable degrades to ``"unknown"`` parts
    rather than ever echoing the input.
    """
    host = "unknown"
    dbname = "unknown"
    if "://" in dsn:
        try:
            parts = urlsplit(dsn)
            host = parts.hostname or "unknown"
            dbname = parts.path.lstrip("/") or "unknown"
        except ValueError:
            pass
    else:
        for token in dsn.split():
            key, sep, value = token.partition("=")
            if not sep:
                continue
            if key == "host":
                host = value or host
            elif key == "dbname":
                dbname = value or dbname
    return f"{host}/{dbname}"


def _run_pg_dump(dsn: str, dump_path: Path) -> None:
    """Run ``pg_dump --format=custom`` into *dump_path*. The DSN is never logged.

    Raises:
        PgDumpNotFoundError: no ``pg_dump`` binary on PATH.
        BackupCreateError: non-zero exit (stderr is scrubbed of the DSN).
    """
    pg_dump = shutil.which("pg_dump")
    if pg_dump is None:
        raise PgDumpNotFoundError(
            "pg_dump not found on PATH — the pg backup profile needs the Postgres "
            "client tools (Debian/Ubuntu: `apt install postgresql-client`; "
            "macOS: `brew install libpq`)"
        )
    proc = subprocess.run(  # noqa: S603 — fixed binary, no shell
        [pg_dump, "--format=custom", "--file", str(dump_path), "--dbname", dsn],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        # Scrub the DSN from anything we surface: it may carry credentials.
        detail = (proc.stderr or "no stderr").replace(dsn, "<dsn redacted>").strip()
        raise BackupCreateError(
            f"pg_dump failed with exit code {proc.returncode}: {detail}"
        )
    if not dump_path.is_file() or dump_path.stat().st_size == 0:
        raise BackupCreateError("pg_dump reported success but produced no dump file")


#: Core metadata-store tables whose dump-time row counts anchor restore
#: verification (ADR-0217 D5). Matches metadata_store.postgres._TENANT_TABLES.
PG_COUNT_TABLES = ("runs", "capsules", "signatures", "retention_policies")


def _collect_object_store(
    stage: Path,
    *,
    adapter: Optional["WormAdapter"],
    backend: Optional[str],
    tenants: Optional[list[str]],
    allow_pending_wal: bool,
    deep: bool,
    wal_db_path: Optional[Path],
) -> tuple[BackupMember, CoverageEntry, tuple[str, str, str]]:
    """Build the manifest-only listing member (ADR-0216 D6).

    Returns (member, coverage row, (arc name, backend tag, fingerprint hash)).
    Refuses while the local WAL has pending un-chained uploads — those
    capsules are invisible to the chain and would be silently lost.
    """
    from novafabric.backup.object_store_manifest import (
        OBJECT_STORE_MANIFEST_NAME,
        BackendFingerprint,
        WalState,
        collect_object_store_listing,
    )

    resolved_backend = backend or os.environ.get("NOVA_OCS_BACKEND")
    resolved_adapter = adapter
    if resolved_adapter is None:
        if not resolved_backend:
            raise BackupCreateError(
                "manifest profile requires an object-store backend — pass "
                "--backend (or set NOVA_OCS_BACKEND)"
            )
        from novafabric.object_capsule_store.backend_router import make_adapter

        try:
            resolved_adapter = make_adapter(backend=resolved_backend)
        except Exception as exc:  # noqa: BLE001 — surface a legible create error
            raise BackupCreateError(
                f"Cannot initialise object-store backend {resolved_backend!r}: {exc}"
            ) from exc
    backend_tag = resolved_backend or (
        type(resolved_adapter).__name__.replace("WormAdapter", "").lower() or "unknown"
    )
    if backend_tag == "inmemory":
        backend_tag = "local"

    wal_state = WalState()
    if wal_db_path is not None and Path(wal_db_path).exists():
        from novafabric.object_capsule_store.local_wal import LocalWal

        pending = LocalWal(wal_db_path).pending_count()
        wal_state = WalState(present=True, pending=pending)
        if pending and not allow_pending_wal:
            raise BackupCreateError(
                f"Local WAL at {wal_db_path} has {pending} pending un-chained "
                "upload(s) — a manifest-only set taken now would silently omit "
                "them. Drain the WAL first, or pass --allow-pending-wal to "
                "record the gap as evidence and proceed."
            )

    fingerprint = BackendFingerprint(
        backend_tag=backend_tag,
        encryption_enabled=os.environ.get("NOVA_OBJECT_STORE_ENCRYPTION") == "1",
    )
    listing = collect_object_store_listing(
        resolved_adapter,
        fingerprint,
        tenants=tenants,
        deep=deep,
        wal_state=wal_state,
    )
    listing_path = stage / OBJECT_STORE_MANIFEST_NAME
    listing_path.write_text(
        json.dumps(listing.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    member = _member(
        listing_path,
        OBJECT_STORE_MANIFEST_NAME,
        kind="object_manifest",
        role="object-store",
    )
    detail = (
        f"{listing.totals['runs']} run chain(s) across "
        f"{listing.totals['tenants']} tenant(s) pinned "
        f"({listing.totals['commits']} commit(s))"
    )
    if wal_state.pending:
        detail += f"; WARNING: {wal_state.pending} WAL upload(s) not yet chained"
    coverage_row = CoverageEntry(component="object-store", status="included", detail=detail)
    return member, coverage_row, (
        OBJECT_STORE_MANIFEST_NAME,
        backend_tag,
        listing.backend_fingerprint.fingerprint_sha256,
    )


def _pg_verification_fields(
    dsn: str,
) -> tuple[Optional[str], Optional[dict[str, int]]]:
    """Dump-time alembic revision + per-table counts, or (None, None).

    Best-effort by design: psycopg is an optional extra and the dump itself
    needs only the client tools. Failures never fail the backup — restore
    reports the skipped verification honestly instead.
    """
    try:
        import psycopg
    except ImportError:
        return None, None
    try:
        with psycopg.connect(dsn) as conn:
            revision: Optional[str] = None
            try:
                row = conn.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()
                revision = str(row[0]) if row else None
            except psycopg.Error:
                conn.rollback()  # table may not exist on older deployments
            counts: dict[str, int] = {}
            for table in PG_COUNT_TABLES:
                try:
                    row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
                    counts[table] = int(row[0]) if row else 0
                except psycopg.Error:
                    conn.rollback()
            return revision, counts or None
    except psycopg.Error:
        return None, None


def _pg_client_version() -> Optional[str]:
    """``pg_dump --version`` output for version-skew diagnostics (ADR-0217 D6)."""
    pg_dump = shutil.which("pg_dump")
    if pg_dump is None:
        return None
    proc = subprocess.run(  # noqa: S603 — fixed binary, no shell
        [pg_dump, "--version"], capture_output=True, text=True
    )
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def _read_schema_revision(snapshot_db: Path) -> Optional[str]:
    """Registry schema revision from the snapshot, or None when uninitialised."""
    conn = sqlite3.connect(snapshot_db)
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return str(row[0]) if row and row[0] is not None else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _member(
    file_path: Path,
    arc_path: str,
    *,
    kind: str,
    role: Optional[str] = None,
    origin: str = "home",
    sensitive: bool = False,
) -> BackupMember:
    digest = hashlib.sha256()
    with open(file_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return BackupMember(
        path=arc_path,
        sha256=digest.hexdigest(),
        size_bytes=file_path.stat().st_size,
        kind=kind,  # type: ignore[arg-type]
        role=role,
        origin=origin,  # type: ignore[arg-type]
        sensitive=sensitive,
    )


def _sign_manifest(manifest: BackupManifest) -> tuple[BackupManifest, Optional[bytes]]:
    """Sign the manifest body if a local NovaSeal profile is configured.

    Two truthful outcomes (worm-conformance honesty pattern): a genuine DSSE
    envelope + ``signing_status: "signed"``, or ``signature: null`` +
    ``signing_status: "unsigned"`` with the reason in ``signing_detail``.
    Never a bare hash presented as a signature.
    """
    try:
        profile = load_signing_profile()
    except SealConfigError as exc:
        return _unsigned(manifest, f"NovaSeal config error: {exc}"), None

    if profile is None:
        return _unsigned(manifest, _UNSIGNED_DETAIL), None
    if profile.profile != "local" or profile.key_path is None or profile.cert_path is None:
        return _unsigned(
            manifest,
            f"NovaSeal profile {profile.profile!r} is configured, but backup-set "
            "signing supports the local profile only in this slice.",
        ), None

    payload = manifest.signable_bytes()
    try:
        dsse_bytes = create_envelope(payload, profile.key_path, profile.cert_path)
    except EnvelopeError as exc:
        return _unsigned(manifest, f"NovaSeal signing failed: {exc}"), None

    envelope = json.loads(dsse_bytes)
    sig_entry = envelope["signatures"][0]
    signed = manifest.model_copy(
        update={
            "signing_status": "signed",
            "signing_detail": None,
            "signature": ManifestSignature(
                key_id=sig_entry["keyid"],
                algorithm="ecdsa-p256-sha256",
                sig=sig_entry["sig"],
            ),
        }
    )
    return signed, dsse_bytes


def _unsigned(manifest: BackupManifest, detail: str) -> BackupManifest:
    return manifest.model_copy(
        update={"signing_status": "unsigned", "signing_detail": detail, "signature": None}
    )


def _write_archive(
    archive_path: Path,
    manifest_path: Path,
    dsse_path: Optional[Path],
    members: list[BackupMember],
    stage: Path,
    *,
    allow_keys: bool = False,
) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(manifest_path, arcname=MANIFEST_NAME)
            if dsse_path is not None:
                tar.add(dsse_path, arcname=MANIFEST_DSSE_NAME)
            for member in members:
                # Defense in depth: the deny-filter already ran at collection
                # time; refuse to archive a denied path no matter how it got in.
                # ``allow_keys`` tracks the manifest's includes_keys (ADR-0216
                # D4): only opted-in external key members may pass.
                if is_denied(member.path, allow_keys=allow_keys):
                    raise BackupCreateError(
                        f"Refusing to archive excluded path {member.path!r} "
                        "(key material / secrets must never enter a backup set)"
                    )
                tar.add(stage / member.path, arcname=member.path)
    except OSError as exc:
        raise BackupCreateError(f"Cannot write backup archive {archive_path}: {exc}") from exc
