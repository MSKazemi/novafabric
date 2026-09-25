"""Portable session bundle (ADR-0122 P4 remainder, experimental).

One ZIP carrying a session's ``session.json`` plus every referenced member
Run Capsule, with a digest index, so an air-gapped reviewer can check the
whole session with nothing but the archive (ADR-0122 D4).

Archive layout::

    session.json                   the session manifest, byte-for-byte
    capsules/<run_id>/...          each member capsule, byte-for-byte
    session-bundle.json            digest index (written last)

``session-bundle.json`` deliberately reuses the Evidence Bundle's
verification primitives rather than inventing new ones: an ``artifacts[]``
list of ``{path, sha256, size_bytes}``, a ``manifest_hash`` computed exactly
like the Evidence Bundle's (sorted-key compact JSON minus the field), and a
per-member ``capsule_hash`` that is the same RFC 6962 Merkle root an Evidence
Bundle ``subject`` records (:func:`novafabric.evidence.merkle.capsule_merkle_root`).
It is a transport packaging of two existing records (a session manifest and
Run Capsules), not a signed artifact: for signed evidence over the members,
export them with ``nova evidence export``.

Guarantees:

- **Export refuses rather than ships a hole** — an empty session, a
  ``missing``/``tampered`` member, a symlink inside a capsule, or a non-ULID
  ``run_id`` (it becomes a path) is a :class:`SessionBundleError`.
- **Deterministic bytes** — entries sorted by archive path, pinned
  timestamps (1980-01-01) and permissions, no wall-clock field in the index:
  the same session + capsules produce a byte-identical archive.
- **Atomic, bounded writes** — the archive is streamed to a sibling temp file
  and ``os.replace``-d into place; file bodies are copied in fixed chunks.
- **Verification is total** — every listed digest recomputed, unlisted
  files rejected, ``session.json`` digest and ordering re-checked, every
  member's ``capsule.yaml`` digest matched to its ``capsule_ref`` and its
  Merkle root recomputed.
- **Import is guarded** — absolute / ``..`` / backslash / drive-letter member
  names, duplicates, symlink entries, entries whose compression ratio marks
  them as a zip bomb, and archives past the entry-count or byte ceilings are
  refused before anything lands in the sessions root; extraction re-counts
  the bytes actually inflated rather than trusting declared sizes. A
  non-ULID ``session_id`` (it becomes the destination directory name) fails
  verification, and import re-checks that the destination is a direct child
  of the sessions root. A verified bundle is staged and renamed into place
  atomically.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from pydantic import BaseModel, Field

from novafabric.session.manifest import (
    SESSION_MANIFEST_FILENAME,
    SessionError,
    SessionIntegrityError,
    SessionManifest,
    load_session,
    parse_capsule_ref,
    sessions_root,
    validate_ordering,
)
from novafabric.session.view import BUNDLED_CAPSULES_DIRNAME, resolve_members

SESSION_BUNDLE_VERSION = "0.1.0"
#: The digest index inside the archive.
BUNDLE_INDEX_FILENAME = "session-bundle.json"
#: Pinned ZIP timestamp (the earliest a ZIP can encode) for deterministic bytes.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_CHUNK = 1 << 16
_ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")

VERIFIER_INSTRUCTIONS = (
    "Verification recipe (no NovaFabric required):\n"
    "  1. Recompute manifest_hash: drop the field from session-bundle.json,\n"
    "     canonicalize the rest as sorted-key JSON with no whitespace, sha256\n"
    "     it, prefix with 'sha256:'.\n"
    "  2. For each entry in `artifacts[]`, recompute sha256 of the file at\n"
    "     `path` and compare; the archive must hold no other files.\n"
    "  3. Confirm sha256(session.json) equals `session_manifest.sha256`.\n"
    "  4. For each member, confirm sha256(capsules/<run_id>/capsule.yaml)\n"
    "     equals the digest in its `capsule_ref` in session.json.\n"
)


class SessionBundleError(SessionError):
    """A session bundle cannot be exported, verified, or imported."""


class UnsafeBundleMemberError(SessionBundleError):
    """An archive member name or type could escape the extraction root."""


class BundleLimits(BaseModel):
    """Resource ceilings for export and verification (bounded by default)."""

    max_entries: int = Field(default=100_000, gt=0)
    max_total_bytes: int = Field(default=8 * 1024**3, gt=0)
    #: Largest ``file_size / compress_size`` accepted for one archive entry
    #: whose declared size exceeds :attr:`ratio_check_min_bytes` (zip-bomb guard).
    max_compression_ratio: float = Field(default=100.0, gt=0)
    #: Entries at or below this declared size skip the ratio check (small
    #: files of repeated bytes compress absurdly well and are harmless).
    ratio_check_min_bytes: int = Field(default=1024**2, ge=0)


class BundleMember(BaseModel):
    """One member capsule's record in ``session-bundle.json``."""

    run_id: str
    sequence: int
    capsule_ref: str
    path: str
    capsule_hash: str


class SessionBundleExport(BaseModel):
    """What :func:`export_session_bundle` wrote."""

    path: str
    session_id: str
    members: int
    files: int
    archive_sha256: str


class SessionBundleVerification(BaseModel):
    """Outcome of :func:`verify_session_bundle`; ``ok`` iff ``problems`` is empty."""

    bundle: str
    ok: bool
    session_id: str | None = None
    members: int = 0
    files_checked: int = 0
    problems: list[str] = Field(default_factory=list)


class SessionBundleImport(BaseModel):
    """What :func:`import_session_bundle` placed in the sessions root."""

    session_id: str
    session_dir: str
    members: int


# ---------------------------------------------------------------------------
# Shared primitives (reused from the Evidence Bundle, imported lazily so the
# session package stays cheap to import)
# ---------------------------------------------------------------------------


def _canonical_hash(index: dict[str, Any]) -> str:
    from novafabric.evidence.bundle import _canonical_manifest_hash

    return _canonical_manifest_hash(index)


def _merkle_root(capsule_dir: Path) -> str:
    from novafabric.evidence.merkle import capsule_merkle_root

    return capsule_merkle_root(capsule_dir)


def _zip_info(arcname: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(arcname, date_time=_ZIP_EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3  # unix — pinned so the header bytes never vary by host
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


def _dir_info(arcname: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(arcname, date_time=_ZIP_EPOCH)
    info.create_system = 3
    info.external_attr = ((stat.S_IFDIR | 0o755) << 16) | 0x10  # MS-DOS dir bit
    return info


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _capsule_files(capsule_dir: Path) -> list[tuple[str, Path | None]]:
    """Sorted ``(posix-relpath, path)`` of every file; ``(dir/, None)`` per empty dir.

    Empty directories (a capsule's ``inputs/``/``outputs/`` often are) travel
    as explicit directory entries so an imported capsule keeps its layout.

    Raises:
        SessionBundleError: The capsule contains a symlink.
    """
    found: list[tuple[str, Path | None]] = []
    for dirpath, dirnames, filenames in os.walk(capsule_dir, followlinks=False):
        current = Path(dirpath)
        for name in dirnames + filenames:
            candidate = current / name
            if candidate.is_symlink():
                raise SessionBundleError(
                    f"{candidate} is a symlink — a bundle carries bytes, not "
                    "links that resolve differently on the reviewer's machine"
                )
        if not dirnames and not filenames and current != capsule_dir:
            found.append((current.relative_to(capsule_dir).as_posix() + "/", None))
        for name in filenames:
            path = current / name
            found.append((path.relative_to(capsule_dir).as_posix(), path))
    return sorted(found, key=lambda item: item[0])


def _plan_entries(
    manifest: SessionManifest, root: Path | None, capsule_base: Path | None
) -> tuple[list[tuple[str, Path | None]], list[tuple[BundleMember, Path]]]:
    resolved = resolve_members(manifest, root=root, capsule_base=capsule_base)
    problems = [
        f"turn {r.member.sequence} (run {r.member.run_id}) is {r.status}"
        for r in resolved
        if r.status != "ok"
    ]
    if problems:
        raise SessionBundleError(
            f"session {manifest.session_id} cannot be bundled — "
            + "; ".join(problems)
            + " (a bundle must carry every member it claims)"
        )
    entries: list[tuple[str, Path | None]] = []
    members: list[tuple[BundleMember, Path]] = []
    for r in resolved:
        run_id = r.member.run_id
        if not _ULID_RE.match(run_id):
            raise SessionBundleError(
                f"member run_id {run_id!r} is not a ULID — refusing to use it as an archive path"
            )
        assert r.capsule_dir is not None  # status == "ok" implies located
        capsule_dir = Path(r.capsule_dir)
        prefix = f"{BUNDLED_CAPSULES_DIRNAME}/{run_id}"
        for rel, path in _capsule_files(capsule_dir):
            entries.append((f"{prefix}/{rel}", path))
        members.append(
            (
                BundleMember(
                    run_id=run_id,
                    sequence=r.member.sequence,
                    capsule_ref=r.member.capsule_ref,
                    path=f"{prefix}/",
                    capsule_hash=_merkle_root(capsule_dir),
                ),
                capsule_dir,
            )
        )
    return entries, members


def _open_regular_nofollow(source: Path) -> BinaryIO:
    """Open *source* for binary reading without following a final symlink.

    ``O_NONBLOCK`` keeps a FIFO swapped in after planning from hanging the
    open; the ``fstat`` check then refuses anything but a regular file.

    Raises:
        SessionBundleError: *source* is (now) a symlink, not a regular file,
            or no longer readable.
    """
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        fd = os.open(source, flags)
    except OSError as exc:
        kind = "became a symlink" if source.is_symlink() else f"cannot be read ({exc})"
        raise SessionBundleError(f"{source} {kind} during export — refusing to bundle it") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SessionBundleError(f"{source} is not a regular file — refusing to bundle it")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _write_entry(
    zf: zipfile.ZipFile, arcname: str, source: Path, budget: list[int]
) -> dict[str, Any]:
    """Stream *source* into the archive; return its ``artifacts[]`` entry.

    *source* is re-opened with ``O_NOFOLLOW`` and ``fstat``-checked, so a file
    swapped for a symlink (or anything but a regular file) after
    :func:`_capsule_files` planned it is refused rather than followed.
    """
    digest = hashlib.sha256()
    size = 0
    src = _open_regular_nofollow(source)
    size_hint = os.fstat(src.fileno()).st_size
    with (
        src,
        zf.open(_zip_info(arcname), "w", force_zip64=size_hint >= (1 << 31)) as dst,
    ):
        while chunk := src.read(_CHUNK):
            size += len(chunk)
            budget[0] -= len(chunk)
            if budget[0] < 0:
                raise SessionBundleError(
                    "session exceeds the bundle byte ceiling "
                    "(raise BundleLimits.max_total_bytes to export it)"
                )
            digest.update(chunk)
            dst.write(chunk)
    return {"path": arcname, "sha256": "sha256:" + digest.hexdigest(), "size_bytes": size}


def export_session_bundle(
    session_id: str,
    output_path: Path,
    root: Path | None = None,
    capsule_base: Path | None = None,
    limits: BundleLimits | None = None,
) -> SessionBundleExport:
    """Write *session_id* and all its member capsules as one verifiable ZIP.

    Reads only — never modifies the manifest or any member capsule.

    Raises:
        SessionNotFoundError: No manifest exists for *session_id*.
        SessionIntegrityError: The manifest is malformed.
        SessionBundleError: The session is empty, a member is missing or
            tampered (or changes during export), a capsule holds a symlink, or
            a resource ceiling is exceeded. No partial archive is left behind.
    """
    limits = limits or BundleLimits()
    manifest = load_session(session_id, root=root)
    if not manifest.member_runs:
        raise SessionBundleError(
            f"session {session_id} has no members — a bundle of nothing would "
            "verify and read as a successful export"
        )
    manifest_file = sessions_root(root) / session_id / SESSION_MANIFEST_FILENAME
    manifest_bytes = manifest_file.read_bytes()
    entries, members = _plan_entries(manifest, root, capsule_base)
    if len(entries) + 2 > limits.max_entries:
        raise SessionBundleError(
            f"session {session_id} needs {len(entries) + 2} archive entries, over "
            f"the ceiling of {limits.max_entries}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    os.close(fd)
    tmp = Path(tmp_name)
    budget = [limits.max_total_bytes]
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            artifacts: list[dict[str, Any]] = []
            ordered = sorted(
                [(SESSION_MANIFEST_FILENAME, manifest_file), *entries],
                key=lambda item: item[0],
            )
            for arcname, source in ordered:
                if source is None:  # an empty directory, kept for layout
                    zf.writestr(_dir_info(arcname), b"")
                elif source == manifest_file:
                    zf.writestr(_zip_info(arcname), manifest_bytes)
                    artifacts.append(
                        {
                            "path": arcname,
                            "sha256": "sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
                            "size_bytes": len(manifest_bytes),
                        }
                    )
                else:
                    artifacts.append(_write_entry(zf, arcname, source, budget))
            _check_members_unchanged(artifacts, members)
            index = _build_index(manifest, manifest_bytes, members, artifacts)
            zf.writestr(
                _zip_info(BUNDLE_INDEX_FILENAME),
                json.dumps(index, indent=2, sort_keys=True) + "\n",
            )
        os.replace(tmp, output_path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return SessionBundleExport(
        path=str(output_path),
        session_id=session_id,
        members=len(members),
        files=len(artifacts),
        archive_sha256=_file_sha256(output_path),
    )


def _check_members_unchanged(
    artifacts: list[dict[str, Any]], members: list[tuple[BundleMember, Path]]
) -> None:
    """The ``capsule.yaml`` bytes actually archived must still match each ref."""
    by_path = {a["path"]: a["sha256"] for a in artifacts}
    for member, _capsule_dir in members:
        _prefix, want = parse_capsule_ref(member.capsule_ref)
        got = by_path.get(f"{member.path}capsule.yaml")
        if got != want:
            raise SessionBundleError(
                f"member {member.run_id} changed during export (capsule.yaml "
                f"digest {got}, capsule_ref expects {want}) — refusing to ship it"
            )


def _build_index(
    manifest: SessionManifest,
    manifest_bytes: bytes,
    members: list[tuple[BundleMember, Path]],
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    from novafabric import __version__

    index: dict[str, Any] = {
        "bundle_kind": "session-bundle",
        "schema_version": SESSION_BUNDLE_VERSION,
        "session_id": manifest.session_id,
        "session_manifest": {
            "path": SESSION_MANIFEST_FILENAME,
            "sha256": "sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
        },
        "members": [m.model_dump() for m, _ in members],
        "artifacts": artifacts,
        "created_by": {"name": "novafabric", "version": __version__},
        "verifier_instructions": VERIFIER_INSTRUCTIONS,
    }
    index["manifest_hash"] = _canonical_hash(index)
    return index


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


# ---------------------------------------------------------------------------
# Verify / import
# ---------------------------------------------------------------------------


def safe_member_path(name: str) -> PurePosixPath:
    """Validate one archive member name; return it as a relative posix path.

    Raises:
        UnsafeBundleMemberError: Absolute, ``..``, empty, backslash, or
            drive-letter names — anything that could land outside the
            extraction root on any platform.
    """
    if not name or name.startswith(("/", "~")) or "\\" in name:
        raise UnsafeBundleMemberError(f"unsafe archive member name {name!r}")
    raw_parts = name.split("/")
    if any(p in ("..", ".", "") for p in raw_parts):
        raise UnsafeBundleMemberError(f"unsafe archive member name {name!r}")
    pure = PurePosixPath(name)
    if pure.is_absolute():  # pragma: no cover - leading '/' already refused
        raise UnsafeBundleMemberError(f"unsafe archive member name {name!r}")
    if any(":" in p for p in pure.parts):
        raise UnsafeBundleMemberError(f"unsafe archive member name {name!r}")
    return pure


def _check_ratio(info: zipfile.ZipInfo, limits: BundleLimits) -> None:
    """Refuse an entry whose declared expansion ratio marks it as a zip bomb."""
    if info.file_size <= limits.ratio_check_min_bytes:
        return
    ratio = info.file_size / info.compress_size if info.compress_size else float("inf")
    if ratio > limits.max_compression_ratio:
        raise SessionBundleError(
            f"archive member {info.filename!r} expands {ratio:.0f}x "
            f"({info.compress_size} -> {info.file_size} bytes), over the "
            f"compression-ratio ceiling of {limits.max_compression_ratio:g}x (zip bomb?)"
        )


def _guard_infolist(
    zf: zipfile.ZipFile, limits: BundleLimits
) -> tuple[list[zipfile.ZipInfo], list[PurePosixPath]]:
    """Central-directory prechecks: names, types, duplicates, ceilings.

    Returns:
        The file entries, and the (validated) directory entries.
    """
    infos = zf.infolist()
    if len(infos) > limits.max_entries:
        raise SessionBundleError(
            f"archive has {len(infos)} entries, over the ceiling of {limits.max_entries}"
        )
    seen: set[str] = set()
    files: list[zipfile.ZipInfo] = []
    dirs: list[PurePosixPath] = []
    declared = 0
    for info in infos:
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise UnsafeBundleMemberError(f"archive member {info.filename!r} is a symlink")
        if info.is_dir():
            dirs.append(safe_member_path(info.filename.rstrip("/")))
            continue
        safe_member_path(info.filename)
        if info.filename in seen:
            raise UnsafeBundleMemberError(f"archive member {info.filename!r} appears twice")
        seen.add(info.filename)
        _check_ratio(info, limits)
        declared += info.file_size
        files.append(info)
    if declared > limits.max_total_bytes:
        raise SessionBundleError(
            f"archive declares {declared} bytes, over the ceiling of {limits.max_total_bytes}"
        )
    return files, dirs


def _extract(
    zf: zipfile.ZipFile,
    files: list[zipfile.ZipInfo],
    dirs: list[PurePosixPath],
    dest: Path,
    limits: BundleLimits,
) -> None:
    """Streamed extraction; re-measures bytes so a lying header cannot bomb."""
    for directory in dirs:
        dest.joinpath(*directory.parts).mkdir(parents=True, exist_ok=True)
    budget = limits.max_total_bytes
    for info in files:
        target = dest.joinpath(*safe_member_path(info.filename).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with zf.open(info) as src, target.open("wb") as dst:
            # Count the bytes actually inflated, never the declared size:
            # zipfile bounds reads by the header today, but the ceiling must
            # hold even if a reader (or a forged header) does not.
            while chunk := src.read(_CHUNK):
                written += len(chunk)
                budget -= len(chunk)
                if written > info.file_size or budget < 0:
                    raise SessionBundleError(
                        f"archive member {info.filename!r} expands past its "
                        "declared size or the byte ceiling"
                    )
                dst.write(chunk)


def _check_index(payload: Path, report: SessionBundleVerification) -> dict[str, Any] | None:
    index_file = payload / BUNDLE_INDEX_FILENAME
    if not index_file.is_file():
        report.problems.append(f"no {BUNDLE_INDEX_FILENAME} — not a NovaFabric session bundle")
        return None
    try:
        index = json.loads(index_file.read_text(encoding="utf-8"))
    except ValueError as exc:
        report.problems.append(f"{BUNDLE_INDEX_FILENAME} is not valid JSON: {exc}")
        return None
    if not isinstance(index, dict) or index.get("bundle_kind") != "session-bundle":
        report.problems.append(f"{BUNDLE_INDEX_FILENAME} is not a session-bundle index")
        return None
    if index.get("manifest_hash") != _canonical_hash(index):
        report.problems.append("manifest_hash does not match the bundle index (index edited)")
    return index


def _check_artifacts(
    payload: Path, index: dict[str, Any], report: SessionBundleVerification
) -> None:
    artifacts = index.get("artifacts") or []
    listed: set[str] = set()
    for art in artifacts:
        rel = str(art.get("path", ""))
        listed.add(rel)
        try:
            path = payload.joinpath(*safe_member_path(rel).parts)
        except UnsafeBundleMemberError as exc:
            report.problems.append(str(exc))
            continue
        if not path.is_file():
            report.problems.append(f"missing: {rel}")
            continue
        report.files_checked += 1
        if _file_sha256(path) != art.get("sha256"):
            report.problems.append(f"modified: {rel}")
    on_disk = {p.relative_to(payload).as_posix() for p in payload.rglob("*") if p.is_file()}
    for extra in sorted(on_disk - listed - {BUNDLE_INDEX_FILENAME}):
        report.problems.append(f"not in index: {extra}")


def _check_session(payload: Path, index: dict[str, Any], report: SessionBundleVerification) -> None:
    manifest_file = payload / SESSION_MANIFEST_FILENAME
    if not manifest_file.is_file():
        report.problems.append(f"missing: {SESSION_MANIFEST_FILENAME}")
        return
    recorded = (index.get("session_manifest") or {}).get("sha256")
    if _file_sha256(manifest_file) != recorded:
        report.problems.append(f"{SESSION_MANIFEST_FILENAME} digest does not match the index")
    try:
        manifest = SessionManifest.model_validate(
            json.loads(manifest_file.read_text(encoding="utf-8"))
        )
        validate_ordering(manifest)
    except (ValueError, SessionIntegrityError) as exc:
        report.problems.append(f"{SESSION_MANIFEST_FILENAME} is invalid: {exc}")
        return
    report.session_id = manifest.session_id
    if not _ULID_RE.match(manifest.session_id):
        report.problems.append(
            f"session_id {manifest.session_id!r} is not a ULID — refusing to use it "
            "as a directory name"
        )
    if index.get("session_id") != manifest.session_id:
        report.problems.append("index session_id does not match session.json")

    indexed = {str(m.get("run_id")): m for m in index.get("members") or []}
    report.members = len(manifest.member_runs)
    for member in manifest.member_runs:
        entry = indexed.pop(member.run_id, None)
        if entry is None or not _ULID_RE.match(member.run_id):
            report.problems.append(f"member {member.run_id} is not carried by the bundle")
            continue
        capsule_dir = payload / BUNDLED_CAPSULES_DIRNAME / member.run_id
        capsule_yaml = capsule_dir / "capsule.yaml"
        try:
            _prefix, want = parse_capsule_ref(member.capsule_ref)
        except SessionIntegrityError as exc:
            report.problems.append(f"member {member.run_id}: {exc}")
            continue
        if not capsule_yaml.is_file():
            report.problems.append(f"member {member.run_id}: capsule.yaml missing")
            continue
        if _file_sha256(capsule_yaml) != want:
            report.problems.append(
                f"member {member.run_id}: capsule.yaml does not match its capsule_ref"
            )
        if _merkle_root(capsule_dir) != entry.get("capsule_hash"):
            report.problems.append(f"member {member.run_id}: capsule_hash mismatch")
        if entry.get("sequence") != member.sequence:
            report.problems.append(f"member {member.run_id}: sequence mismatch")
    for stray in sorted(indexed):
        report.problems.append(f"index lists {stray}, which session.json does not reference")


def _verify_into(
    bundle_path: Path, payload: Path, limits: BundleLimits
) -> SessionBundleVerification:
    report = SessionBundleVerification(bundle=str(bundle_path), ok=False)
    try:
        with zipfile.ZipFile(bundle_path) as zf:
            files, dirs = _guard_infolist(zf, limits)
            _extract(zf, files, dirs, payload, limits)
    except (zipfile.BadZipFile, OSError) as exc:
        report.problems.append(f"not a readable ZIP: {exc}")
        return report
    except SessionBundleError as exc:
        report.problems.append(str(exc))
        return report
    index = _check_index(payload, report)
    if index is not None:
        _check_artifacts(payload, index, report)
        _check_session(payload, index, report)
    report.ok = not report.problems
    return report


def verify_session_bundle(
    bundle_path: Path, limits: BundleLimits | None = None
) -> SessionBundleVerification:
    """Verify a session bundle end-to-end without importing it.

    Extracts into a private temp directory (guarded and bounded), then runs
    every check in the module docstring. Never raises for content problems —
    they are listed in :attr:`SessionBundleVerification.problems`.
    """
    limits = limits or BundleLimits()
    with tempfile.TemporaryDirectory(prefix="nova-session-bundle-") as tmp:
        return _verify_into(bundle_path, Path(tmp), limits)


def import_session_bundle(
    bundle_path: Path,
    root: Path | None = None,
    limits: BundleLimits | None = None,
) -> SessionBundleImport:
    """Verify *bundle_path*, then place it at ``<sessions-root>/<session_id>/``.

    The members land at ``<session_dir>/capsules/<run_id>/``, where
    ``nova session show``/``replay`` resolve them (digest-gated). Nothing is
    written to the sessions root unless verification passes; the verified
    payload is staged inside the root and renamed into place atomically.

    Raises:
        SessionBundleError: Verification failed (the message lists every
            problem) or the session already exists locally.
    """
    limits = limits or BundleLimits()
    base = sessions_root(root)
    base.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".import-", dir=base))
    try:
        payload = staging / "payload"
        payload.mkdir()
        report = _verify_into(bundle_path, payload, limits)
        if not report.ok or report.session_id is None:
            raise SessionBundleError(
                f"refusing to import {bundle_path}: " + "; ".join(report.problems)
            )
        (payload / BUNDLE_INDEX_FILENAME).unlink()
        destination = base / report.session_id
        # Defence in depth behind the ULID check in verification: the
        # destination must be a direct child of the sessions root.
        if destination.resolve().parent != base.resolve():
            raise SessionBundleError(
                f"refusing to import {bundle_path}: session_id {report.session_id!r} "
                f"would place the session outside {base}"
            )
        try:
            # Exclusive claim: a concurrent import of the same session fails here.
            destination.mkdir()
        except FileExistsError as exc:
            raise SessionBundleError(
                f"session {report.session_id} already exists at {destination} — "
                "remove it first; an import never overwrites local evidence"
            ) from exc
        try:
            os.replace(payload, destination)  # replaces only the empty dir we claimed
        except BaseException:
            # Release the claim so a retry is not blocked by our empty dir;
            # rmdir only succeeds while it is still empty (i.e. still ours).
            try:
                destination.rmdir()
            except OSError:
                pass
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    from novafabric.session.index import upsert_session

    upsert_session(report.session_id, root=root)
    return SessionBundleImport(
        session_id=report.session_id,
        session_dir=str(destination),
        members=report.members,
    )
