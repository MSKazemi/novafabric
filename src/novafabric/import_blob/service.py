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

"""Verified batch import of an ADR-0141 export (ADR-0207 P1).

``import_batch`` runs the spec's ordered pipeline: source layout → manifest
structure → signature → internal consistency → member bytes → classification →
hardened staged unpack → reindex → receipt. Steps 1–5 run before **any** write
to the capsule store; any failure refuses the whole import and still writes a
receipt recording the refusal.

Everything cryptographic or format-defining is reused from ``export_blob``:
``verify_export_manifest`` (the signed-mode gate, whole), ``pack_capsule``
(content-addressed idempotency), ``compute_batch_digest`` /
``LocalDirDestination`` (unsigned-mode hash checks over the same primitives).
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal

from novafabric._paths import default_capsule_dir, nova_home
from novafabric.audit import AUDIT_LOG_PATH, AuditEventType, AuditLog
from novafabric.capture._ulid import new_ulid
from novafabric.export_blob.destinations import LocalDirDestination
from novafabric.export_blob.digest import compute_batch_digest
from novafabric.export_blob.models import MANIFEST_FILENAME, ExportManifest
from novafabric.export_blob.packing import CapsulePackError, pack_capsule
from novafabric.export_blob.service import VerifyStatus, verify_export_manifest
from novafabric.import_blob.models import (
    CollisionDetail,
    ImportCounts,
    ImportProducer,
    ImportReceipt,
    MemberRecord,
    ReindexInfo,
    VerificationInfo,
)
from novafabric.import_blob.unpack import UnpackError, safe_extract_tar
from novafabric.object_capsule_store.cas import compute_sha256

log = logging.getLogger(__name__)

_EXPORT_MANIFEST_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3] / "schemas" / "export-manifest.schema.json"
)

#: Spec batch-import-v0 §Exit codes.
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INVALID = 3
EXIT_INCOMPLETE = 4
EXIT_COLLISION = 5
EXIT_FAILED_MEMBERS = 6

_ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz")

_Status = Literal["VALID", "INVALID", "INCOMPLETE"]


class ImportUsageError(Exception):
    """The import cannot even start (bad flags / unreadable or unsafe source)."""


@dataclass
class ImportOutcome:
    """Everything one import run produced: receipt, its path, exit code."""

    receipt: ImportReceipt
    receipt_path: Path | None
    exit_code: int


def _now_rfc3339() -> str:
    return (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _pkg_version() -> str:
    try:
        return version("novafabric")
    except PackageNotFoundError:  # pragma: no cover — dev tree without metadata
        return "0.0.0"


def _write_receipt(receipt: ImportReceipt, receipts_dir: Path | None) -> Path:
    directory = (
        receipts_dir if receipts_dir is not None else nova_home() / "import-receipts"
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{receipt.import_id}.json"
    path.write_text(json.dumps(receipt.to_json_dict(), indent=2) + "\n")
    return path


def _append_audit(receipt: ImportReceipt, audit_log_path: Path | None) -> None:
    """One hash-chained audit entry per run — never capsule content (D7)."""
    path = audit_log_path if audit_log_path is not None else AUDIT_LOG_PATH
    try:
        AuditLog(path).append(
            event_type=AuditEventType.CAPSULE_IMPORT,
            actor="cli-user",
            resource_id=receipt.import_id,
            details={
                "export_id": receipt.export_id,
                "counts": receipt.counts.model_dump(),
                "verification_mode": receipt.verification.mode,
                "verification_status": receipt.verification.status,
                "dry_run": receipt.dry_run,
            },
        )
    except OSError as exc:  # pragma: no cover — audit must not mask the receipt
        log.warning("import %s: audit append failed: %s", receipt.import_id, exc)


def _verify_unsigned(
    manifest: ExportManifest, src_dir: Path
) -> tuple[_Status, list[str]]:
    """Steps 4–5 without the signature: same primitives, same refusals (D2).

    Only *authorship* goes unverified — tamper still refuses.
    """
    problems: list[str] = []
    if manifest.count != len(manifest.members):
        problems.append(
            f"count={manifest.count} does not equal "
            f"len(members)={len(manifest.members)}"
        )
    recomputed = compute_batch_digest(manifest.members)
    if recomputed != manifest.batch_digest:
        problems.append(
            f"batch_digest mismatch: stored {manifest.batch_digest}, "
            f"recomputed {recomputed}"
        )
    if problems:
        return "INVALID", problems

    source = LocalDirDestination(src_dir)
    for member in manifest.members:
        data = source.get_blob(member.content_hash)
        if data is None:
            problems.append(
                f"member missing at source: {member.capsule_id} "
                f"({member.content_hash})"
            )
            continue
        actual = "sha256:" + compute_sha256(data)
        if actual != member.content_hash:
            problems.append(
                f"member content mismatch: {member.capsule_id} "
                f"expected {member.content_hash}, got {actual}"
            )
            continue
        if len(data) != member.size:
            problems.append(
                f"member size mismatch: {member.capsule_id} "
                f"expected {member.size}, got {len(data)}"
            )
    if problems:
        return "INCOMPLETE", problems
    return "VALID", problems


def _staged_parent_run_id(staged_dir: Path) -> str | None:
    """``parent_run_id`` from a staged capsule's ``capsule.yaml`` (best-effort)."""
    import yaml  # noqa: PLC0415

    try:
        meta = yaml.safe_load((staged_dir / "capsule.yaml").read_text())
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(meta, dict):
        return None
    parent = meta.get("parent_run_id")
    return str(parent) if parent else None


def _parents_first(ids: list[str], parent_of: dict[str, str | None]) -> list[str]:
    """Order *ids* so any in-batch parent precedes its children (spec SHOULD).

    Bounded, recursion-free topological pass; a cycle (impossible in honest
    data) degrades to the original order for the remainder.
    """
    id_set = set(ids)
    ordered: list[str] = []
    placed: set[str] = set()
    remaining = list(ids)
    for _ in range(len(ids)):
        still: list[str] = []
        for cid in remaining:
            parent = parent_of.get(cid)
            if parent is None or parent not in id_set or parent in placed:
                ordered.append(cid)
                placed.add(cid)
            else:
                still.append(cid)
        if len(still) == len(remaining):  # pragma: no cover — cycle guard
            ordered.extend(still)
            return ordered
        remaining = still
        if not remaining:
            break
    return ordered


def _reindex_capsule(
    capsule_dir: Path,
    conn: sqlite3.Connection | None,
    reindex_info: ReindexInfo,
    db_path: Path | None,
) -> None:
    """Lineage + runs-cache for one imported capsule (D6, best-effort).

    Failures are recorded per capsule but never remove the unpacked capsule:
    the store is the source of truth, indexes are derived.
    """
    from novafabric.lineage._importer import index_capsule_lineage  # noqa: PLC0415
    from novafabric.registry.runs_cache import upsert_run  # noqa: PLC0415
    from novafabric.serve.capsule_loader import load_capsule_manifest  # noqa: PLC0415

    capsule_id = capsule_dir.name
    try:
        result = index_capsule_lineage(capsule_dir, db_path=db_path)
        if result.warning:
            reindex_info.errors.append(f"lineage {capsule_id}: {result.warning}")
        elif not result.skipped:
            reindex_info.lineage_capsules += 1
    except Exception as exc:  # noqa: BLE001 — indexes are derived, never fatal
        reindex_info.errors.append(f"lineage {capsule_id}: {exc}")

    if conn is None:
        return
    try:
        manifest = load_capsule_manifest(capsule_dir)
        summary = {
            "run_id": manifest.get("run_id", capsule_id),
            "status": manifest.get("status"),
            "created_at": manifest.get("created_at"),
            "finished_at": manifest.get("finished_at"),
            "duration_ms": manifest.get("duration_ms"),
            "exit_code": manifest.get("exit_code"),
            "model_call_count": manifest.get("model_call_count", 0),
            "tool_call_count": manifest.get("tool_call_count", 0),
            "mutating_tool_count": manifest.get("mutating_tool_count", 0),
            "command": manifest.get("command", []),
            "novafabric_version": manifest.get("novafabric_version"),
            "capsule_path": str(capsule_dir.resolve()),
        }
        upsert_run(conn, summary)
        # Commit per capsule: holding a write transaction open across the
        # next capsule's lineage indexing (a separate connection on the same
        # SQLite file) deadlocks WAL's single-writer lock for its 5 s busy
        # timeout. Per-capsule commits keep the two writers interleaved.
        conn.commit()
        reindex_info.runs_cache_rows += 1
    except Exception as exc:  # noqa: BLE001 — indexes are derived, never fatal
        reindex_info.errors.append(f"runs_cache {capsule_id}: {exc}")


def _resolve_source(
    source: Path,
) -> tuple[Path | None, str | None, tempfile.TemporaryDirectory[str] | None]:
    """Resolve *source* to a layout directory (extracting archive sources).

    Returns ``(dir, problem, tmp)``: ``problem`` is set (and ``dir`` None) for
    an archive that fails to extract (step 1 → ``INVALID``); ``tmp`` is the
    temp dir keeping an extracted archive alive (caller cleans up). Raises
    :class:`ImportUsageError` for a path that is neither a directory nor a
    ``.tar``/``.tar.gz``/``.tgz`` file (usage, exit 2).
    """
    if source.is_dir():
        return source, None, None
    if not source.is_file():
        raise ImportUsageError(f"source does not exist: {source}")
    if not source.name.endswith(_ARCHIVE_SUFFIXES):
        raise ImportUsageError(
            f"source must be a directory or a .tar/.tar.gz archive: {source}"
        )
    tmp = tempfile.TemporaryDirectory(prefix="nova-import-")
    target = Path(tmp.name) / "source"
    try:
        safe_extract_tar(source.read_bytes(), target)
    except (UnpackError, OSError) as exc:
        return None, f"archive source failed to extract: {exc}", tmp
    # The exporter writes the manifest at the layout root; a courier archive
    # may wrap the layout in one top-level directory — accept that too.
    if not (target / MANIFEST_FILENAME).is_file():
        children = [p for p in target.iterdir() if p.is_dir()]
        if len(children) == 1 and (children[0] / MANIFEST_FILENAME).is_file():
            return children[0], None, tmp
    return target, None, tmp


def _guard_source_not_in_store(src_dir: Path, capsule_root: Path) -> None:
    """Importing a store into itself is always a mistake (spec §Edge cases)."""
    src = src_dir.resolve()
    root = capsule_root.resolve()
    if src.is_relative_to(root) or root.is_relative_to(src):
        raise ImportUsageError(
            f"source {src} overlaps the capsule store {root}: "
            "importing a store into itself is refused"
        )


def import_batch(
    source: Path,
    *,
    capsule_root: Path | None = None,
    public_key_pem: bytes | None = None,
    allow_unsigned: bool = False,
    dry_run: bool = False,
    reindex: bool = True,
    receipts_dir: Path | None = None,
    db_path: Path | None = None,
    audit_log_path: Path | None = None,
) -> ImportOutcome:
    """Ingest an ADR-0141 export at *source* into the local capsule store.

    Verification-first and fail-closed (spec batch-import-v0): steps 1–5 run
    before any store write; refusals (``INVALID``/``INCOMPLETE``) import
    nothing and still write a receipt. Idempotent by content address; a
    same-``run_id``-different-content member is a per-member ``collision`` that
    is never written. Raises :class:`ImportUsageError` for exit-code-2
    conditions (bad flag combination, unreadable/unsafe source).

    *db_path* overrides the registry database (lineage + runs cache live in
    the same ``registry.db``); *receipts_dir* / *audit_log_path* override the
    receipt directory and audit log (defaults:
    ``$NOVAFABRIC_HOME/import-receipts`` and the standard audit log).
    """
    if public_key_pem is None and not allow_unsigned:
        raise ImportUsageError(
            "no verifying key: pass --public-key <pem> (from "
            "`nova export-blob --public-key-out`) or --allow-unsigned to skip "
            "the signature check only"
        )
    if public_key_pem is not None and allow_unsigned:
        raise ImportUsageError(
            "--public-key and --allow-unsigned are mutually exclusive: "
            "pass exactly one"
        )

    resolved_root = capsule_root if capsule_root is not None else default_capsule_dir()
    started_at = _now_rfc3339()
    import_id = new_ulid()
    mode: Literal["signed", "unsigned"] = (
        "signed" if public_key_pem is not None else "unsigned"
    )

    def _finish(
        *,
        verification: VerificationInfo,
        exit_code: int,
        export_id: str | None = None,
        batch_digest: str | None = None,
        members: list[MemberRecord] | None = None,
        counts: ImportCounts | None = None,
        reindex_info: ReindexInfo | None = None,
    ) -> ImportOutcome:
        receipt = ImportReceipt(
            import_id=import_id,
            dry_run=dry_run,
            source=str(source),
            export_id=export_id,
            batch_digest=batch_digest,
            verification=verification,
            members=members or [],
            counts=counts or ImportCounts(),
            reindex=reindex_info or ReindexInfo(),
            started_at=started_at,
            finished_at=_now_rfc3339(),
            producer=ImportProducer(version=_pkg_version()),
        )
        path = _write_receipt(receipt, receipts_dir)
        _append_audit(receipt, audit_log_path)
        return ImportOutcome(receipt, path, exit_code)

    def _refuse(
        status: _Status,
        problems: list[str],
        exit_code: int,
        manifest: ExportManifest | None = None,
    ) -> ImportOutcome:
        return _finish(
            verification=VerificationInfo(mode=mode, status=status, problems=problems),
            exit_code=exit_code,
            export_id=manifest.export_id if manifest else None,
            batch_digest=manifest.batch_digest if manifest else None,
            members=[
                MemberRecord(
                    capsule_id=m.capsule_id,
                    content_hash=m.content_hash,
                    action="not_processed",
                )
                for m in (manifest.members if manifest else [])
            ],
        )

    src_dir, layout_problem, tmp_dir = _resolve_source(source)
    try:
        if src_dir is None:
            return _refuse(
                "INVALID", [layout_problem or "unreadable source"], EXIT_INVALID
            )
        _guard_source_not_in_store(src_dir, resolved_root)

        # Step 1 — source layout: manifest present and parses.
        manifest_path = src_dir / MANIFEST_FILENAME
        try:
            raw = json.loads(manifest_path.read_text())
        except (OSError, ValueError) as exc:
            return _refuse(
                "INVALID", [f"manifest unreadable/invalid: {exc}"], EXIT_INVALID
            )

        # Step 2 — manifest structure against the closed ADR-0141 schema.
        import jsonschema  # type: ignore[import-untyped]  # noqa: PLC0415

        validator = jsonschema.Draft202012Validator(
            json.loads(_EXPORT_MANIFEST_SCHEMA_PATH.read_text())
        )
        schema_errors = [
            "manifest schema violation at "
            f"/{'/'.join(str(p) for p in error.path)}: {error.message}"
            for error in validator.iter_errors(raw)
        ]
        if schema_errors:
            return _refuse("INVALID", schema_errors, EXIT_INVALID)
        manifest = ExportManifest.model_validate(raw)

        # Steps 3–5 — signature (signed mode: verify_export_manifest, whole —
        # not a reimplementation), internal consistency, member bytes.
        if public_key_pem is not None:
            report = verify_export_manifest(
                manifest_path, public_key_pem, dest_override=str(src_dir)
            )
            status: _Status
            if report.status is VerifyStatus.INVALID:
                status = "INVALID"
            elif report.status is VerifyStatus.INCOMPLETE:
                status = "INCOMPLETE"
            else:
                status = "VALID"
            problems = list(report.problems)
        else:
            status, problems = _verify_unsigned(manifest, src_dir)

        verification = VerificationInfo(mode=mode, status=status, problems=problems)
        if status != VerifyStatus.VALID.value:
            exit_code = EXIT_INVALID if status == "INVALID" else EXIT_INCOMPLETE
            return _refuse(status, problems, exit_code, manifest)

        # Step 6 — member classification (content-addressed idempotency, D4/D5).
        records: dict[str, MemberRecord] = {}
        to_import: list[str] = []
        batch_ids = {m.capsule_id for m in manifest.members}
        for member in manifest.members:
            target = resolved_root / member.capsule_id
            if target.is_dir():
                try:
                    local_hash = "sha256:" + compute_sha256(pack_capsule(target))
                except (CapsulePackError, OSError) as exc:
                    records[member.capsule_id] = MemberRecord(
                        capsule_id=member.capsule_id,
                        content_hash=member.content_hash,
                        action="failed",
                        detail=f"cannot re-pack existing capsule: {exc}",
                    )
                    continue
                if local_hash == member.content_hash:
                    action: Literal["skipped_existing", "collision"] = (
                        "skipped_existing"
                    )
                    detail: CollisionDetail | None = None
                else:
                    action = "collision"
                    detail = CollisionDetail(
                        local_hash=local_hash, manifest_hash=member.content_hash
                    )
                records[member.capsule_id] = MemberRecord(
                    capsule_id=member.capsule_id,
                    content_hash=member.content_hash,
                    action=action,
                    detail=detail,
                )
            else:
                to_import.append(member.capsule_id)
                records[member.capsule_id] = MemberRecord(
                    capsule_id=member.capsule_id,
                    content_hash=member.content_hash,
                    action="imported",  # provisional; may become "failed" below
                )

        member_by_id = {m.capsule_id: m for m in manifest.members}
        imported_dirs: list[Path] = []

        # Step 7 — hardened staged unpack, atomic move (wet runs only, D3).
        if not dry_run and to_import:
            source_dest = LocalDirDestination(src_dir)
            staging_root = resolved_root / ".import-staging" / import_id
            try:
                staged: list[str] = []
                parent_of: dict[str, str | None] = {}
                for capsule_id in to_import:
                    member = member_by_id[capsule_id]
                    record = records[capsule_id]
                    data = source_dest.get_blob(member.content_hash)
                    # Defense-in-depth TOCTOU re-hash (verified in step 5).
                    if data is None or (
                        "sha256:" + compute_sha256(data) != member.content_hash
                    ):
                        record.action = "failed"
                        record.detail = "blob changed or vanished after verification"
                        continue
                    staged_dir = staging_root / capsule_id
                    try:
                        safe_extract_tar(data, staged_dir)
                    except UnpackError as exc:
                        record.action = "failed"
                        record.detail = str(exc)
                        shutil.rmtree(staged_dir, ignore_errors=True)
                        continue
                    parent_of[capsule_id] = _staged_parent_run_id(staged_dir)
                    staged.append(capsule_id)

                # Atomic renames, parents before children (spec §Edge cases).
                for capsule_id in _parents_first(staged, parent_of):
                    record = records[capsule_id]
                    target = resolved_root / capsule_id
                    parent = parent_of.get(capsule_id)
                    try:
                        resolved_root.mkdir(parents=True, exist_ok=True)
                        (staging_root / capsule_id).rename(target)
                    except OSError as exc:
                        record.action = "failed"
                        record.detail = f"atomic move failed: {exc}"
                        continue
                    imported_dirs.append(target)
                    if (
                        parent is not None
                        and parent not in batch_ids
                        and not (resolved_root / parent).is_dir()
                    ):
                        record.detail = (
                            f"orphan parent_run_id: {parent} "
                            "(in neither the batch nor the store)"
                        )
            finally:
                shutil.rmtree(staging_root, ignore_errors=True)
                staging_parent = resolved_root / ".import-staging"
                if staging_parent.is_dir() and not any(staging_parent.iterdir()):
                    staging_parent.rmdir()

        # Step 8 — reindex what is persistent (D6); the in-memory query index
        # self-scans and is intentionally untouched.
        reindex_info = ReindexInfo()
        if not dry_run and reindex and imported_dirs:
            from novafabric.registry.runs_cache import (  # noqa: PLC0415
                ensure_runs_cache,
            )
            from novafabric.registry.store import get_connection  # noqa: PLC0415

            conn: sqlite3.Connection | None
            try:
                conn = get_connection(db_path)
                ensure_runs_cache(conn)
            except (sqlite3.Error, OSError) as exc:
                reindex_info.errors.append(f"runs_cache connection: {exc}")
                conn = None
            for capsule_dir in imported_dirs:
                _reindex_capsule(capsule_dir, conn, reindex_info, db_path)
            if conn is not None:
                conn.commit()
                conn.close()

        members = [records[m.capsule_id] for m in manifest.members]
        counts = ImportCounts(
            imported=sum(1 for r in members if r.action == "imported"),
            skipped_existing=sum(
                1 for r in members if r.action == "skipped_existing"
            ),
            collisions=sum(1 for r in members if r.action == "collision"),
            failed=sum(1 for r in members if r.action == "failed"),
        )
        if counts.collisions:
            exit_code = EXIT_COLLISION
        elif counts.failed:
            exit_code = EXIT_FAILED_MEMBERS
        else:
            exit_code = EXIT_OK

        return _finish(
            verification=verification,
            exit_code=exit_code,
            export_id=manifest.export_id,
            batch_digest=manifest.batch_digest,
            members=members,
            counts=counts,
            reindex_info=reindex_info,
        )
    finally:
        if tmp_dir is not None:
            tmp_dir.cleanup()
