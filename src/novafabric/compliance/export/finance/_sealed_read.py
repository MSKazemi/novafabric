"""Bounded, symlink-refusing reads of sealed capsule evidence (ADR-0159 D6 finance collectors).

Module-private helper shared by :mod:`.adverse_action_collect` and :mod:`.cat_collect` so both
exporters enforce one rule set:

* a capsule file is opened read-only with ``O_NOFOLLOW`` + ``O_NONBLOCK`` — a symlink is corrupt
  evidence (never followed) and a FIFO swapped in after the ``lstat`` can never block the read;
  the opened descriptor must still be the same regular file the ``lstat`` saw (no TOCTOU swap);
* reads are bounded — ``limit + 1`` bytes are read, never more, and overflow is corrupt;
* a path whose digest is recorded in the manifest's ADR-0251 ``evidence_digests`` map is *sealed*:
  if it is absent, a symlink, or not a regular file (FIFO, directory, …) the sealed evidence has
  vanished, which is corrupt — never silently reported as an absent source. Only a path that is
  both absent **and** unrecorded is a legitimate gap (``None``).
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

__all__ = ["CorruptCapsuleError", "hash_regular_file", "read_sealed", "sha256_bytes"]

_CHUNK = 1024 * 1024
_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


class CorruptCapsuleError(Exception):
    """The capsule exists but its sealed evidence is unreadable, malformed, or mismatched."""


def sha256_bytes(data: bytes) -> str:
    """``'sha256:<hex>'`` over ``data``."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _open_regular(path: Path, what: str, st: os.stat_result) -> int:
    """Open ``path`` without following a symlink; the fd must be the file ``st`` described."""
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except OSError as exc:
        raise CorruptCapsuleError(f"cannot read {what} {path}: {exc}") from exc
    try:
        fst = os.fstat(fd)
    except OSError as exc:  # pragma: no cover - fstat of an fd we just opened
        os.close(fd)
        raise CorruptCapsuleError(f"cannot read {what} {path}: {exc}") from exc
    if not stat.S_ISREG(fst.st_mode) or (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
        os.close(fd)
        raise CorruptCapsuleError(f"{what} {path} changed while being opened")
    return fd


def _lstat_regular(path: Path, what: str, sealed: bool) -> os.stat_result | None:
    """``lstat`` ``path``: ``None`` for a legitimate (unsealed) gap, else a regular file's stat.

    Raises:
        CorruptCapsuleError: a symlink; a sealed path that is absent or not a regular file; or
            a path that cannot be stat'ed.
    """
    try:
        st = path.lstat()
    except FileNotFoundError:
        if sealed:
            raise CorruptCapsuleError(
                f"{what} {path} is sealed in evidence_digests but absent from the capsule"
            ) from None
        return None
    except OSError as exc:
        raise CorruptCapsuleError(f"cannot stat {what} {path}: {exc}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise CorruptCapsuleError(f"{what} {path} is a symlink; capsule evidence is never followed")
    if not stat.S_ISREG(st.st_mode):
        if sealed:
            raise CorruptCapsuleError(
                f"{what} {path} is sealed in evidence_digests but is not a regular file"
            )
        return None
    return st


def read_sealed(
    capsule_dir: Path, rel: str, limit: int, what: str, digests: dict[str, str]
) -> bytes | None:
    """Read capsule file ``rel`` (bounded, no symlinks) and check it against ``digests``.

    Returns ``None`` only when ``rel`` is absent / not a regular file **and** has no recorded
    digest. An unrecorded regular file is returned unchecked (the caller reports it unbound).

    Raises:
        CorruptCapsuleError: a symlink; a sealed file that is missing or not a regular file; an
            unreadable file; more than ``limit`` bytes; or bytes that do not match the recorded
            digest.
    """
    path = capsule_dir / rel
    recorded = digests.get(rel)
    st = _lstat_regular(path, what, recorded is not None)
    if st is None:
        return None
    fd = _open_regular(path, what, st)
    try:
        with os.fdopen(fd, "rb") as fh:
            data = fh.read(limit + 1)
    except OSError as exc:  # pragma: no cover - read of an open regular file
        raise CorruptCapsuleError(f"cannot read {what} {path}: {exc}") from exc
    if len(data) > limit:
        raise CorruptCapsuleError(f"{what} {path} exceeds its read bound (limit {limit} bytes)")
    if recorded is not None and recorded != sha256_bytes(data):
        raise CorruptCapsuleError(
            f"{rel} does not match its sealed evidence_digests entry (recorded {recorded})"
        )
    return data


def hash_regular_file(path: Path, what: str) -> tuple[str, int]:
    """Stream-hash regular file ``path`` (no symlinks) → ``('sha256:<hex>', size)``.

    Raises:
        CorruptCapsuleError: ``path`` is a symlink, not a regular file, or unreadable.
    """
    st = _lstat_regular(path, what, sealed=True)
    if st is None:  # pragma: no cover - sealed=True never returns None
        raise CorruptCapsuleError(f"{what} {path} is not a regular file")
    fd = _open_regular(path, what, st)
    h = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "rb") as fh:
            while chunk := fh.read(_CHUNK):
                h.update(chunk)
                size += len(chunk)
    except OSError as exc:  # pragma: no cover - read of an open regular file
        raise CorruptCapsuleError(f"cannot read {what} {path}: {exc}") from exc
    return "sha256:" + h.hexdigest(), size
