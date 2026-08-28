"""Hardened capsule-ingest helpers (ADR-0203 P1, experimental).

Implements the normative streaming algorithm from
``the private design/spec/ingest-hardening-v0.md`` for ``POST /v0/capsules``:

- ``Content-Length`` fast-path rejection (413 ``payload_too_large``);
- chunked spool-to-disk body read with authoritative byte counting;
- ZIP central-directory prechecks and streamed, re-measured zip-bomb guards
  (422 ``zip_guard_violation`` with ``details.reason``);
- member-name guard closing the ADR-0203 Context §4 zip-slip traversal;
- streamed member extraction (never ``zf.read(member)``) into a caller-owned
  temp directory, so peak memory is O(``spool_chunk_bytes``).

Everything is bounded: one spool file per in-flight request, chunked reads,
chunked decompression, no unbounded buffering. Stdlib only (ADR-0024).
"""

from __future__ import annotations

import io
import shutil
import tempfile
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from novafabric.server.config import IngestConfig
from novafabric.server.errors import PayloadTooLargeError, ValidationError

# ``details.reason`` vocabulary (closed set, spec v0).
REASON_ENTRY_COUNT = "entry_count"
REASON_TOTAL_UNCOMPRESSED = "total_uncompressed"
REASON_COMPRESSION_RATIO = "compression_ratio"
REASON_UNSAFE_MEMBER_NAME = "unsafe_member_name"
REASON_NO_CAPSULE_ROOT = "no_capsule_root"

#: Ratio floor (normative): the compression-ratio guard is evaluated only once
#: the decompressed size exceeds this — tiny, highly compressible files must
#: not trip it.
RATIO_FLOOR_BYTES = 1_048_576  # 1 MiB

#: Spool + temp-extract directory, kept on the same filesystem as the capsule
#: store so the final publish rename is atomic.
SPOOL_DIR_NAME = ".ingest-tmp"


# --------------------------------------------------------------------------- #
# Error builders
# --------------------------------------------------------------------------- #


def _too_large(limit: int, received: int) -> PayloadTooLargeError:
    return PayloadTooLargeError(
        f"Upload exceeds max_upload_bytes ({limit}).",
        details={"limit_bytes": limit, "received_bytes": received},
    )


def _guard_violation(
    reason: str,
    message: str,
    limit: float,
    observed: float,
    member: str | None = None,
) -> ValidationError:
    details: dict[str, Any] = {"reason": reason, "limit": limit, "observed": observed}
    if member is not None:
        details["member"] = member
    return ValidationError(message, code="zip_guard_violation", details=details)


# --------------------------------------------------------------------------- #
# Step 1 — Content-Length fast path
# --------------------------------------------------------------------------- #


def check_content_length(content_length: str | None, limits: IngestConfig) -> None:
    """Reject a declared-oversize request before reading any body bytes.

    ``received_bytes`` in the envelope is the *declared* length on this path.
    A missing or unparseable header falls through to the authoritative
    counted-stream path in :func:`spool_upload`.
    """
    if not limits.max_upload_bytes or content_length is None:
        return
    try:
        declared = int(content_length)
    except ValueError:
        return
    if declared > limits.max_upload_bytes:
        raise _too_large(limits.max_upload_bytes, declared)


# --------------------------------------------------------------------------- #
# Step 2 — chunked spool to disk (authoritative size enforcement)
# --------------------------------------------------------------------------- #


def reap_orphaned_spools(spool_dir: Path, *, started_at: float) -> int:
    """Delete ingest temporaries that cannot belong to any live request.

    The spool directory holds two kinds of transient: the ``.spool`` file the
    body streams into, and the ``<run_id>.<hex>`` directory the archive extracts
    into. Both are owned by one request and removed on every exit path — but a
    crash between creation and cleanup strands them, and nothing ever reclaimed
    them. One such orphan was measured surviving 52 minutes and a full service
    restart on a busy hub (B9). The leak is small per event and unbounded in time.

    Both kinds are reaped. Reaping only the file was the first cut, and a live
    crash-injection run on Azure showed why that is not enough: killing the hub
    during extraction reclaimed a 201 MB spool and left a 28 MB extraction
    directory behind for good. A partial reaper still leaks, just more slowly.

    *started_at* is this process's start time: anything older predates the
    current server and therefore has no owner. Entries at or after it may be in
    flight and are never touched. Everything under the spool directory is
    transient by construction — published capsules live in the capsule store,
    reached by an atomic rename out of here — so age is a sufficient test.

    Fail-open — a missing directory or an unreadable entry returns/skips rather
    than raising, because reclamation must never prevent the server starting.
    Returns the number of entries removed.
    """
    removed = 0
    try:
        entries = list(spool_dir.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            is_dir = entry.is_dir()
            if not is_dir and entry.suffix != ".spool":
                continue
            if entry.stat().st_mtime >= started_at:
                continue  # may belong to an in-flight request
            if is_dir:
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink()
            removed += 1
        except OSError:  # pragma: no cover — racing cleanup or permissions
            continue
    return removed


async def spool_upload(
    upload: UploadFile, spool_dir: Path, limits: IngestConfig
) -> tuple[Path, int]:
    """Stream the upload to a named spool file in bounded chunks.

    Returns ``(spool_path, received_bytes)``. The caller owns the spool file
    and must delete it on every exit path. Raises
    :class:`PayloadTooLargeError` the moment the byte count exceeds
    ``max_upload_bytes`` — this holds even when ``Content-Length`` is absent
    or lies; the partial spool is removed before raising.
    """
    spool_dir.mkdir(parents=True, exist_ok=True)
    cap = limits.max_upload_bytes
    received = 0
    with tempfile.NamedTemporaryFile(
        dir=spool_dir, suffix=".spool", delete=False
    ) as fh:
        spool_path = Path(fh.name)
        try:
            while True:
                chunk = await upload.read(limits.spool_chunk_bytes)
                if not chunk:
                    break
                received += len(chunk)
                if cap and received > cap:
                    raise _too_large(cap, received)
                fh.write(chunk)
        except BaseException:
            fh.close()
            spool_path.unlink(missing_ok=True)
            raise
    return spool_path, received


# --------------------------------------------------------------------------- #
# Step 3 — central-directory prechecks (advisory; step 5 re-measures)
# --------------------------------------------------------------------------- #


def precheck_central_directory(zf: zipfile.ZipFile, limits: IngestConfig) -> None:
    """Reject early on attacker-declared metadata; streaming re-verifies."""
    infos = zf.infolist()
    if limits.zip_max_entries and len(infos) > limits.zip_max_entries:
        raise _guard_violation(
            REASON_ENTRY_COUNT,
            f"Archive has {len(infos)} entries; zip_max_entries is "
            f"{limits.zip_max_entries}.",
            limits.zip_max_entries,
            len(infos),
        )
    if limits.zip_max_uncompressed_bytes:
        declared = sum(i.file_size for i in infos)
        if declared > limits.zip_max_uncompressed_bytes:
            raise _guard_violation(
                REASON_TOTAL_UNCOMPRESSED,
                f"Archive declares {declared} uncompressed bytes; "
                f"zip_max_uncompressed_bytes is "
                f"{limits.zip_max_uncompressed_bytes}.",
                limits.zip_max_uncompressed_bytes,
                declared,
            )


# --------------------------------------------------------------------------- #
# Step 4 — guarded single-member read (manifest peek)
# --------------------------------------------------------------------------- #


def read_member_guarded(
    zf: zipfile.ZipFile, name: str, limits: IngestConfig
) -> bytes:
    """Chunked decompression of one member with the per-member guards live.

    Used only for the ``capsule.yaml`` peek that must happen before
    extraction (run_id resolution + duplicate/orphan checks). Memory is
    bounded by the zip guards, not by the archive's declared sizes.
    """
    info = zf.getinfo(name)
    compressed = max(info.compress_size, 1)
    out = io.BytesIO()
    produced = 0
    with zf.open(info) as src:
        while True:
            chunk = src.read(limits.spool_chunk_bytes)
            if not chunk:
                break
            produced += len(chunk)
            _check_member_guards(limits, name, produced, compressed)
            out.write(chunk)
    return out.getvalue()


def _check_member_guards(
    limits: IngestConfig, name: str, produced: int, compressed: int
) -> None:
    """Per-member caps, enforced as decompressed bytes are produced."""
    if (
        limits.zip_max_uncompressed_bytes
        and produced > limits.zip_max_uncompressed_bytes
    ):
        raise _guard_violation(
            REASON_TOTAL_UNCOMPRESSED,
            f"Member '{name}' exceeded zip_max_uncompressed_bytes "
            f"({limits.zip_max_uncompressed_bytes}) while decompressing.",
            limits.zip_max_uncompressed_bytes,
            produced,
            member=name,
        )
    if limits.zip_max_ratio and produced > RATIO_FLOOR_BYTES:
        ratio = produced / compressed
        if ratio > limits.zip_max_ratio:
            raise _guard_violation(
                REASON_COMPRESSION_RATIO,
                f"Member '{name}' expanded past {limits.zip_max_ratio:g}:1 "
                f"(observed {ratio:.1f}:1).",
                limits.zip_max_ratio,
                round(ratio, 1),
                member=name,
            )


# --------------------------------------------------------------------------- #
# Step 5 — member-name guard + streamed extraction
# --------------------------------------------------------------------------- #


def archive_strip_top(names: Iterable[str]) -> bool:
    """Whether one leading path component may be dropped from every member.

    True only when every *file* member shares a single top-level directory —
    the ``<run_id>/capsule.yaml`` layout produced by ``nova capsule export``,
    where that directory is packaging and not part of the capsule.

    Any member sitting at the archive root, or a second distinct top-level
    directory, makes the strip destructive: ``outputs/stdout.txt`` would become
    ``stdout.txt`` and could overwrite a sibling that flattens onto the same
    name (ADR-0260). In those shapes nothing may be dropped.

    Deliberately does not validate names — the per-member ``..``/absolute-path
    guard in :func:`safe_member_relpath` remains the sole authority on safety.
    """
    tops: set[str] = set()
    for name in names:
        if name.endswith("/"):
            continue
        parts = Path(name).parts
        if parts and parts[0] in ("/", "\\"):
            parts = parts[1:]
        if len(parts) < 2:
            return False  # a member at the archive root
        tops.add(parts[0])
        if len(tops) > 1:
            return False
    return len(tops) == 1


def safe_member_relpath(name: str, *, strip_top: bool = True) -> Path | None:
    """Extraction-relative path for a member, or ``None`` for directory entries.

    Drops the leading path component when *strip_top* — which the caller
    determines once per archive via :func:`archive_strip_top`, because the
    decision cannot be made correctly from a single member's name.
    Rejects any name containing a ``..`` segment or resolving to an absolute
    path — the ADR-0203 Context §4 zip-slip guard.
    """
    if name.endswith("/"):
        return None
    parts = Path(name).parts
    if ".." in parts:
        raise _guard_violation(
            REASON_UNSAFE_MEMBER_NAME,
            f"Archive member '{name}' contains a '..' path segment.",
            0,
            0,
            member=name,
        )
    # Drop a leading root ('/') so absolute names are treated as relative,
    # matching the historical (incidental) behavior — then strip one level.
    if parts and parts[0] in ("/", "\\"):
        parts = parts[1:]
    if not parts:
        return None
    rel = Path(*parts[1:]) if (strip_top and len(parts) > 1) else Path(*parts)
    if rel.is_absolute():  # pragma: no cover — defense in depth
        raise _guard_violation(
            REASON_UNSAFE_MEMBER_NAME,
            f"Archive member '{name}' resolves to an absolute path.",
            0,
            0,
            member=name,
        )
    return rel


@dataclass
class _GuardState:
    """Running totals for the streamed (authoritative) archive guards."""

    limits: IngestConfig
    entries: int = 0
    total_uncompressed: int = 0
    total_compressed: int = field(default=0)

    def add_entry(self) -> None:
        self.entries += 1
        limit = self.limits.zip_max_entries
        if limit and self.entries > limit:
            raise _guard_violation(
                REASON_ENTRY_COUNT,
                f"Archive exceeded zip_max_entries ({limit}) while extracting.",
                limit,
                self.entries,
            )

    def add_bytes(self, n: int, member: str, member_compressed: int) -> None:
        self.total_uncompressed += n
        limits = self.limits
        if (
            limits.zip_max_uncompressed_bytes
            and self.total_uncompressed > limits.zip_max_uncompressed_bytes
        ):
            raise _guard_violation(
                REASON_TOTAL_UNCOMPRESSED,
                f"Archive exceeded zip_max_uncompressed_bytes "
                f"({limits.zip_max_uncompressed_bytes}) while decompressing.",
                limits.zip_max_uncompressed_bytes,
                self.total_uncompressed,
                member=member,
            )
        if (
            limits.zip_max_ratio
            and self.total_uncompressed > RATIO_FLOOR_BYTES
        ):
            denom = max(self.total_compressed + member_compressed, 1)
            ratio = self.total_uncompressed / denom
            if ratio > limits.zip_max_ratio:
                raise _guard_violation(
                    REASON_COMPRESSION_RATIO,
                    f"Archive total expanded past {limits.zip_max_ratio:g}:1 "
                    f"(observed {ratio:.1f}:1).",
                    limits.zip_max_ratio,
                    round(ratio, 1),
                    member=member,
                )


def extract_archive(
    zf: zipfile.ZipFile, tmp_root: Path, limits: IngestConfig
) -> None:
    """Streamed, guarded extraction of every member into *tmp_root*.

    Never calls ``zf.read(member)`` — each member is decompressed through a
    bounded chunk loop, with the entry-count, total-uncompressed and
    compression-ratio guards re-measured as bytes are produced, aborting
    mid-member on violation. The caller owns *tmp_root* cleanup.
    """
    resolved_root = tmp_root.resolve()
    state = _GuardState(limits=limits)
    strip_top = archive_strip_top(m.filename for m in zf.infolist())
    for member in zf.infolist():
        state.add_entry()
        rel = safe_member_relpath(member.filename, strip_top=strip_top)
        if rel is None:
            continue
        out_path = tmp_root / rel
        if not out_path.resolve().is_relative_to(resolved_root):
            # Defense in depth: unreachable while safe_member_relpath rejects
            # every `..` segment and absolute name (we also never create
            # symlinks), but kept as the normative spec check.
            raise _guard_violation(  # pragma: no cover
                REASON_UNSAFE_MEMBER_NAME,
                f"Archive member '{member.filename}' resolves outside the "
                f"extraction root.",
                0,
                0,
                member=member.filename,
            )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        member_compressed = max(member.compress_size, 1)
        produced = 0
        with zf.open(member) as src, out_path.open("wb") as dst:
            while True:
                chunk = src.read(limits.spool_chunk_bytes)
                if not chunk:
                    break
                produced += len(chunk)
                _check_member_guards(
                    limits, member.filename, produced, member_compressed
                )
                state.add_bytes(len(chunk), member.filename, member_compressed)
                dst.write(chunk)
        state.total_compressed += member.compress_size


def assert_capsule_at_root(tmp_root: Path) -> None:
    """The extracted tree must carry ``capsule.yaml`` at its own root.

    :func:`archive_strip_top` deliberately declines to reshape an archive whose
    members do not share one top-level directory — there is no single root to
    strip, and guessing one merges two namespaces. What is left, though, is a
    directory that is not a capsule: ``capsule.yaml`` sits a level down, every
    reader misses it, and the upload still answered 201. Silent acceptance of an
    unreadable capsule is the same defect ADR-0260 removed, wearing its other
    face, so this rejects instead. Found by driving real capsules through real
    REST ingest on a live cluster; no unit test had posed the shape.
    """
    if (tmp_root / "capsule.yaml").is_file():
        return
    raise _guard_violation(
        REASON_NO_CAPSULE_ROOT,
        "Archive did not yield capsule.yaml at the capsule root: members must "
        "either sit at the archive root or share exactly one top-level directory.",
        0,
        0,
    )
