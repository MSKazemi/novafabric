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

"""Hardened tar extraction for untrusted import archives (ADR-0207 D3).

An import consumes archives produced elsewhere — untrusted input by definition
(ADR-0009). The deterministic packer only ever emits relative, sorted, regular
file/dir entries, but the importer MUST NOT trust the packer: every member name
is sanitized (no absolute paths, no ``..`` components, no drive/UNC prefixes)
and only regular files and directories are extracted — links, devices, and
FIFOs are rejected outright.

Expansion is bounded too (review storage-backup-15): the entry count, the total
uncompressed size and the compression ratio are capped with the server ingest
defaults (``server.config.IngestConfig``), checked from each member's header
*before* its bytes are written, and members are streamed to disk, never read
whole into memory.
"""

from __future__ import annotations

import io
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO

#: Ratios are not judged below this many uncompressed bytes (small archives of
#: tiny, compressible files legitimately exceed any ratio). As server ingest.
_RATIO_FLOOR_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ExtractLimits:
    """Caps on what an untrusted archive may expand to; 0 disables one."""

    max_entries: int = 10_000
    max_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024  # 2 GiB
    max_ratio: float = 100.0


#: The server ingest defaults (``IngestConfig.zip_max_*``).
DEFAULT_LIMITS = ExtractLimits()


class UnpackError(Exception):
    """The archive (or one of its members) is unsafe or unreadable."""


def _validate_member_name(name: str) -> PurePosixPath:
    """Return the sanitized relative path for *name*, or raise :class:`UnpackError`."""
    pure = PurePosixPath(name)
    if pure.is_absolute() or name.startswith(("/", "\\")):
        raise UnpackError(f"unsafe member name (absolute path): {name!r}")
    parts = pure.parts
    if not parts:
        raise UnpackError("unsafe member name (empty)")
    if any(part == ".." for part in parts):
        raise UnpackError(f"unsafe member name ('..' component): {name!r}")
    if any(("\\" in part or ":" in part) for part in parts):
        # Windows-style separators / drive prefixes never appear in honest
        # exports; refuse rather than guess.
        raise UnpackError(f"unsafe member name (suspicious component): {name!r}")
    return pure


def safe_extract_tar(
    source: bytes | Path, target: Path, *, limits: ExtractLimits = DEFAULT_LIMITS
) -> int:
    """Extract a tar archive into *target* with a sanitizing walk; return file count.

    *source* is the archive's bytes, or a path to it (then it is streamed, never
    read whole). Only regular files and directories are written. Any unsafe or
    unsupported member, or one that would exceed *limits*, refuses the **whole**
    archive (an archive that needs one unsafe member dropped is not an archive to
    trust), and *target* is left as-is — the caller stages into a scratch
    directory and discards it on error.
    """
    target.mkdir(parents=True, exist_ok=True)
    resolved_target = target.resolve()
    if isinstance(source, Path):
        compressed = source.stat().st_size
        with open(source, "rb") as fh:
            return _extract(fh, compressed, resolved_target, limits)
    return _extract(io.BytesIO(source), len(source), resolved_target, limits)


def _check_budget(entries: int, total: int, compressed: int, limits: ExtractLimits) -> None:
    if limits.max_entries and entries > limits.max_entries:
        raise UnpackError(f"archive has more than {limits.max_entries} entries; refused")
    if limits.max_uncompressed_bytes and total > limits.max_uncompressed_bytes:
        raise UnpackError(
            f"archive expands past {limits.max_uncompressed_bytes} uncompressed bytes; refused"
        )
    if limits.max_ratio and total > _RATIO_FLOOR_BYTES and total > limits.max_ratio * max(
        compressed, 1
    ):
        raise UnpackError(
            f"archive compression ratio exceeds {limits.max_ratio:g}:1 "
            "(a compression bomb?); refused"
        )


def _extract(fh: IO[bytes], compressed: int, resolved_target: Path, limits: ExtractLimits) -> int:
    extracted = 0
    entries = 0
    total = 0
    try:
        with tarfile.open(fileobj=fh, mode="r|*") as tf:
            for member in tf:
                entries += 1
                total += max(member.size, 0) if member.isreg() else 0
                _check_budget(entries, total, compressed, limits)
                rel = _validate_member_name(member.name)
                dest = resolved_target.joinpath(*rel.parts)
                # Belt-and-braces: the sanitized path must stay inside target.
                # Unreachable after _validate_member_name (no absolute names,
                # no '..', and we never create symlinks) — kept as defense.
                if not dest.resolve().is_relative_to(
                    resolved_target
                ):  # pragma: no cover
                    raise UnpackError(
                        f"member escapes extraction root: {member.name!r}"
                    )
                if member.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                elif member.isreg():
                    fileobj = tf.extractfile(member)
                    if fileobj is None:  # pragma: no cover — isreg implies a body
                        raise UnpackError(f"unreadable member: {member.name!r}")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with open(dest, "wb") as out:
                        shutil.copyfileobj(fileobj, out, length=1024 * 1024)
                    extracted += 1
                else:
                    raise UnpackError(
                        "unsupported member type (links/devices/FIFOs refused): "
                        f"{member.name!r}"
                    )
    except tarfile.TarError as exc:
        raise UnpackError(f"not a readable tar archive: {exc}") from exc
    return extracted
