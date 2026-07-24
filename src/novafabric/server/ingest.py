"""Hardened capsule-ingest helpers (ADR-0203 P1, experimental).

Implements the normative streaming algorithm from
``design/spec/ingest-hardening-v0.md`` for ``POST /v0/capsules``:

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
import tempfile
import zipfile
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


def safe_member_relpath(name: str) -> Path | None:
    """Extraction-relative path for a member, or ``None`` for directory entries.

    Retains the historical behavior of stripping the top-level path component
    (flat ZIPs and single-subdir ZIPs both land flat in the capsule dir).
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
    rel = Path(*parts[1:]) if len(parts) > 1 else Path(parts[0])
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
    for member in zf.infolist():
        state.add_entry()
        rel = safe_member_relpath(member.filename)
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
