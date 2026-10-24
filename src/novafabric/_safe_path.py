"""Turn an untrusted id into a path under a base directory, and write files without following links.

Many NovaFabric ids end up as a file or directory name: a ``run_id`` from an
HTTP request body, a ``capsule_id`` from an import manifest, a ``run_id`` from a
broker event or from a capsule's own ``capsule.yaml``. All of those are
**untrusted input**. Joined with ``base / value``, an absolute value replaces
``base`` outright and a ``..`` value climbs out of it, so the caller writes (or
reads, or bundles) wherever the sender chose.

This module is the one place that turns such an id into a path:

* :func:`require_path_segment` accepts a value only if it is **one** safe path
  segment: a non-empty string with no ``/``, ``\\``, ``..``, NUL or other
  control character, not ``.``, not absolute, at most :data:`MAX_SEGMENT_BYTES`
  bytes. It raises :class:`UnsafePathSegmentError` otherwise.
* :func:`safe_child` joins such a segment onto *base* and checks the result:
  the child may not be a symbolic link (it is checked with ``lstat``, never
  followed), and its real path must sit directly inside the real path of
  *base*. It raises :class:`PathEscapeError` otherwise. *base* itself is the
  caller's own directory and may be reached through a link.

The writers never write *through* a link or a hard link planted at the target
name, because they write a fresh ``O_CREAT | O_EXCL | O_NOFOLLOW`` temporary
file beside it, ``fsync`` it and rename it into place:

* :func:`atomic_write_bytes` replaces the target (mode ``0o644`` by default;
  ``mode=None`` keeps the process umask's ``0o666 & ~umask``, for files that
  must never become wider than a plain ``open()`` would make them);
* :func:`write_private_file` does the same with mode ``0o600``, for signing
  keys and other secrets. With ``exclusive=True`` it refuses an existing target
  (:class:`FileExistsError`) instead of replacing it, so two concurrent creators
  cannot overwrite each other's key;
* :func:`ensure_private_file` creates (or narrows) a ``0o600`` file that another
  library then opens itself — a SQLite database of key material, an append-only
  log — so it is never created at the umask mode.

The mode is set on the open file descriptor before any byte is written, so a
secret is never readable by others, not even for an instant.

Durability: the temporary file is fsynced before the rename and the directory
after it (:func:`fsync_dir`), so after a crash the target holds either the old
bytes or the new ones — never an empty or missing file. A writer that streams
into its own temporary file uses :func:`fsync_file` and :func:`fsync_dir` around
its rename; ``tests/test_rename_sites_are_durable.py`` checks every rename site.

**Limit:** :func:`safe_child` checks the child when it is called. Someone who
can already create links *inside* *base* can swap one in between the check and
the use. No NovaFabric endpoint lets a request create a link, so this needs
local access to the machine; the writers above are immune to it.

No import-time IO, no module state.
"""

from __future__ import annotations

import os
import stat
import unicodedata
from pathlib import Path
from typing import Final

__all__ = [
    "MAX_SEGMENT_BYTES",
    "PathEscapeError",
    "UnsafePathSegmentError",
    "atomic_write_bytes",
    "ensure_private_file",
    "fsync_dir",
    "fsync_file",
    "fsync_tree",
    "is_safe_path_segment",
    "require_path_segment",
    "safe_child",
    "write_private_file",
]

#: The longest name a POSIX filesystem accepts for one path component (NAME_MAX).
MAX_SEGMENT_BYTES: Final[int] = 255

_NOFOLLOW: Final[int] = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC: Final[int] = getattr(os, "O_CLOEXEC", 0)


class UnsafePathSegmentError(ValueError):
    """An id that must be one path segment is not (absolute, ``..``, a separator, NUL ...)."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(
            f"invalid {field}: {reason}; it must be a single name without '/', '\\' or '..'"
        )


class PathEscapeError(ValueError):
    """A joined path is a symbolic link or does not resolve directly inside its base."""


def _segment_problem(value: object) -> str | None:
    """Why *value* is not one safe path segment, or ``None`` if it is."""
    if not isinstance(value, str):
        return "it is not a string"
    if not value:
        return "it is empty"
    if "\x00" in value:
        return "it contains a NUL byte"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return "it contains a control character"
    # Lone surrogates cannot be encoded as a file name, and format/control
    # characters (bidi overrides such as U+202E, zero-width joiners) can make a
    # name display as something it is not. Refuse both as unsafe.
    if any(unicodedata.category(ch) in ("Cs", "Cc", "Cf") for ch in value):
        return "it contains a surrogate, control or invisible formatting character"
    if "/" in value or "\\" in value:
        return "it contains a path separator"
    if value == "." or ".." in value:
        return "it contains '..' or is '.'"
    if os.path.isabs(value):
        return "it is an absolute path"
    if len(value.encode("utf-8")) > MAX_SEGMENT_BYTES:
        return f"it is longer than {MAX_SEGMENT_BYTES} bytes"
    return None


def is_safe_path_segment(value: object) -> bool:
    """True when *value* passes :func:`require_path_segment`."""
    return _segment_problem(value) is None


def require_path_segment(value: object, field: str = "name") -> str:
    """Return *value* if it is one safe path segment, else raise :class:`UnsafePathSegmentError`.

    *field* names the value in the error (``run_id``, ``capsule_id``, ...).
    """
    problem = _segment_problem(value)
    if problem is not None:
        raise UnsafePathSegmentError(field, problem)
    assert isinstance(value, str)  # narrowed by _segment_problem
    return value


def safe_child(base: Path, name: object, *, field: str = "name", suffix: str = "") -> Path:
    """``base / (name + suffix)``, only if *name* is one safe segment and the child stays in *base*.

    *suffix* (for example ``".zip"``) is a fixed string from the caller; the
    combined name must still be one safe segment. The child need not exist.

    Raises:
        UnsafePathSegmentError: *name* (or *name* + *suffix*) is not one safe segment.
        PathEscapeError: the child is a symbolic link, or its real path is not
            directly inside the real path of *base*.
    """
    segment = require_path_segment(name, field)
    leaf = require_path_segment(f"{segment}{suffix}", field) if suffix else segment
    child = Path(base) / leaf
    if os.path.islink(child):
        raise PathEscapeError(f"{field} {segment!r} names a symbolic link; it is not followed")
    real_base = os.path.realpath(base)
    real_child = os.path.realpath(child)
    if os.path.dirname(real_child) != real_base:
        raise PathEscapeError(f"{field} {segment!r} does not resolve inside {base}")
    return child


def fsync_dir(directory: Path) -> None:
    """fsync *directory*, so a rename or create inside it survives a crash.

    Best effort: a platform or filesystem that cannot open or fsync a directory
    is skipped (the rename itself already happened).
    """
    try:
        fd = os.open(directory, os.O_RDONLY | _CLOEXEC)
    except OSError:  # pragma: no cover - a platform without directory fds
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - fsync of a directory is not supported everywhere
        pass
    finally:
        os.close(fd)



def fsync_file(path: Path) -> None:
    """fsync the bytes of an already written, closed file at *path* (not following a link).

    For a writer that streams into its own temporary file (a ZIP, an export) and
    cannot hand its bytes to :func:`atomic_write_bytes`: call it on the
    temporary file before renaming it into place, then :func:`fsync_dir`.
    """
    fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_tree(root: Path) -> None:
    """fsync every regular file and directory under *root*, deepest first; no link is followed.

    Before a whole directory (an uploaded or imported capsule, an imported
    session) is published with one rename, so that after a crash the published
    tree never holds empty or missing files. Call :func:`fsync_dir` on the
    destination's parent after the rename.
    """
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            if stat.S_ISREG(os.lstat(path).st_mode):
                fsync_file(path)
        fsync_dir(Path(dirpath))


def _write_temp(path: Path, data: bytes, mode: int | None) -> Path:
    """Write *data* to a new ``O_EXCL | O_NOFOLLOW`` temp file beside *path*; return its path.

    *mode* ``None``: ``0o666`` filtered by the umask, like a plain ``open()``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC
    for attempt in range(100):
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{attempt}.tmp")
        try:
            fd = os.open(tmp, flags, 0o666 if mode is None else mode)
        except FileExistsError:
            continue
        try:
            if mode is not None:
                os.fchmod(fd, mode)  # the open() mode is filtered by the umask
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            tmp.unlink(missing_ok=True)
            raise
        os.close(fd)
        return tmp
    raise FileExistsError(f"could not create a temporary file beside {path}")


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = 0o644) -> Path:
    """Write *data* to *path* atomically and durably, replacing it, without following a link.

    A link, FIFO or hard link already at *path* is replaced by the rename, never
    written through. *mode* ``None`` gives ``0o666 & ~umask``. Returns *path*.
    """
    path = Path(path)
    tmp = _write_temp(path, data, mode)
    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    fsync_dir(path.parent)
    return path


def write_private_file(path: Path, data: bytes, *, exclusive: bool = False) -> Path:
    """Write a secret to *path* with mode ``0o600``, atomically and without following a link.

    With *exclusive*, an existing *path* (file or link) raises
    :class:`FileExistsError` and is left untouched; otherwise it is replaced.
    Returns *path*.
    """
    path = Path(path)
    if not exclusive:
        return atomic_write_bytes(path, data, mode=0o600)
    tmp = _write_temp(path, data, 0o600)
    try:
        os.link(tmp, path, follow_symlinks=False)  # EEXIST if anything is at *path*
    finally:
        tmp.unlink(missing_ok=True)
    fsync_dir(path.parent)
    return path


def ensure_private_file(path: Path) -> Path:
    """Make *path* a regular file with mode ``0o600``, creating it empty if absent; return it.

    For a secret that something else writes (SQLite opens ``dek.db`` itself; an
    audit log is appended to): call this first, so the file exists at ``0o600``
    before its first byte, instead of being created at the umask mode and
    narrowed afterwards. An existing file keeps its bytes and is narrowed to
    ``0o600``. SQLite creates ``-wal``/``-shm`` sidecars with the database's
    mode, so they follow.

    Raises:
        PathEscapeError: *path* is a symbolic link or not a regular file; a
            secret is never written through one.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_CREAT | _NOFOLLOW | _CLOEXEC
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        if os.path.islink(path):
            raise PathEscapeError(
                f"{path} is a symbolic link; a secret is never written through one"
            ) from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PathEscapeError(f"{path} is not a regular file")
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    return path
