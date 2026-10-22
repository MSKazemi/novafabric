"""Read and copy a Run Capsule directory without ever following a link inside it.

A capsule is a plain directory, and anyone who can write into it can plant a
symbolic link such as ``inputs/data.csv -> ~/.ssh/id_rsa``. Code that copies,
archives or serves capsule files and *follows* that link puts the target's bytes
into its output as if they were capsule content: past redaction, and as the
user running the export. NovaFabric never writes a link into a capsule, and
neither a ZIP entry nor the capsule Merkle root can represent one. So every
capsule exporter, copier and file-serving route refuses links through this one
module instead of each carrying its own walk.

The rules:

* :func:`list_capsule` walks a capsule with ``os.walk(followlinks=False)`` and
  raises :class:`CapsuleLinkError` naming **every** link it finds: dangling, to
  a directory, pointing inside the capsule, or pointing outside it. A link that
  stays inside the capsule is refused too. It cannot be represented in any
  export format, so allowing it would only add a case to reason about.
* :func:`open_capsule_file` opens one file by walking its path one component at
  a time with ``openat(2)`` and ``O_NOFOLLOW``. A link anywhere on the path, in
  a parent directory as well as the final name, is refused at open time. That
  closes the window between a listing and the read: a link planted after
  :func:`list_capsule` ran is still never followed. ``O_NONBLOCK`` plus an
  ``fstat`` check refuses a FIFO or device that was swapped in, so an export
  can never hang on one.
* :func:`copy_capsule_tree` copies through :func:`open_capsule_file` and creates
  each destination file with ``O_CREAT | O_EXCL | O_NOFOLLOW``. It never calls
  ``shutil.copytree``, which re-resolves each path between its symlink check and
  the copy. It then checks the copy once more.

The capsule *root* may itself be reached through a link (``runs/latest``). It
is resolved once, and only links *inside* it are refused, as the evidence export
has always done.

**Known limit: hard links.** A hard link is an ordinary directory entry for a
regular file, so it is copied like any other file. Under Linux's default
``fs.protected_hardlinks = 1`` a user can only hard-link a file they own, or one
they can already read and write. So a planted hard link exposes nothing the
planter could not read already. NovaFabric itself never creates hard links in a
capsule. Refusing every file with ``st_nlink > 1`` would break capsule trees
kept under ``rsync --link-dest`` or ``cp -al`` snapshots, which are routine on
HPC scratch filesystems. Hard links are therefore allowed, and this limit is
documented in ``docs/concepts.md`` ("Links inside a capsule are never followed").
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Final

__all__ = [
    "CapsuleEntryError",
    "CapsuleLinkError",
    "CapsuleListing",
    "CapsuleSpecialFileError",
    "copy_capsule_tree",
    "find_capsule_links",
    "list_capsule",
    "open_capsule_file",
    "read_capsule_file",
    "refuse_capsule_links",
]

_log = logging.getLogger(__name__)

#: Most links named in one refusal message. The count is always given.
MAX_LINKS_REPORTED: Final[int] = 20

_CLOEXEC: Final[int] = getattr(os, "O_CLOEXEC", 0)
_NOFOLLOW: Final[int] = getattr(os, "O_NOFOLLOW", 0)
_DIR_FLAGS: Final[int] = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _CLOEXEC
_FILE_FLAGS: Final[int] = os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | _CLOEXEC
_COPY_CHUNK: Final[int] = 1024 * 1024


class CapsuleEntryError(Exception):
    """A capsule entry cannot be read safely. Base of the two refusals below."""


class CapsuleLinkError(CapsuleEntryError):
    """The capsule holds a symbolic link, and this operation never follows one.

    ``links`` lists each link as ``"<relative path> (<where it points>)"``. It may
    be empty when a single open hit the link and nothing more was walked.
    """

    def __init__(self, message: str, *, links: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.links = links


class CapsuleSpecialFileError(CapsuleEntryError):
    """A capsule path is not a regular file (a FIFO, socket, device or directory)."""


@dataclass(frozen=True)
class CapsuleListing:
    """Every directory and regular file under a capsule, as sorted POSIX paths.

    ``skipped`` holds entries that are neither, such as a FIFO or a socket.
    They are not capsule content, and no exporter has ever included them.
    """

    root: Path
    dirs: tuple[str, ...]
    files: tuple[str, ...]
    skipped: tuple[str, ...]


def _describe_link(path: Path, base: Path) -> str:
    target = Path(os.path.realpath(path))
    if not target.exists():
        where = "dangling"
    elif target == base or base in target.parents:
        where = "points inside the capsule"
    else:
        where = "points outside the capsule"
    return f"{path.relative_to(base).as_posix()} ({where})"


def find_capsule_links(root: Path) -> list[str]:
    """Describe every symlink under *root* without following any of them, sorted."""
    base = root.resolve()
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                found.append(_describe_link(path, base))
    return sorted(found)


def _link_message(capsule: str, links: list[str], operation: str) -> str:
    shown = ", ".join(links[:MAX_LINKS_REPORTED])
    more = len(links) - MAX_LINKS_REPORTED
    return (
        f"capsule {capsule} holds {len(links)} symlink(s); {operation} never "
        f"follows a symlink (its target would be exported as capsule content): {shown}"
        + (f", and {more} more" if more > 0 else "")
        + ". Replace them with regular files or remove them, then try again."
    )


def refuse_capsule_links(
    root: Path,
    *,
    operation: str,
    capsule_name: str | None = None,
    error: type[CapsuleLinkError] = CapsuleLinkError,
) -> None:
    """Raise *error* when *root* holds any symlink. *operation* names the caller."""
    links = find_capsule_links(root)
    if links:
        raise error(
            _link_message(capsule_name or root.name, links, operation), links=tuple(links)
        )


def list_capsule(
    root: Path,
    *,
    operation: str,
    error: type[CapsuleLinkError] = CapsuleLinkError,
) -> CapsuleListing:
    """List *root* without following links; raise *error* if it holds any link."""
    base = root.resolve()
    if not base.is_dir():
        raise NotADirectoryError(f"not a capsule directory: {root}")
    dirs: list[str] = []
    files: list[str] = []
    skipped: list[str] = []
    links: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        here = Path(dirpath)
        for name in dirnames + filenames:
            path = here / name
            rel = path.relative_to(base).as_posix()
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                continue  # removed during the walk: nothing to export
            if stat.S_ISLNK(mode):
                links.append(_describe_link(path, base))
            elif stat.S_ISDIR(mode):
                dirs.append(rel)
            elif stat.S_ISREG(mode):
                files.append(rel)
            else:
                skipped.append(rel)
    if links:
        links.sort()
        raise error(_link_message(root.name, links, operation), links=tuple(links))
    if skipped:
        _log.info(
            "capsule_special_files_skipped",
            extra={"capsule": str(base), "count": len(skipped)},
        )
    return CapsuleListing(
        root=base,
        dirs=tuple(sorted(dirs)),
        files=tuple(sorted(files)),
        skipped=tuple(sorted(skipped)),
    )


def _split_rel(rel: str | PurePosixPath) -> tuple[str, ...]:
    pure = PurePosixPath(rel)
    parts = tuple(p for p in pure.parts if p not in ("", "."))
    if pure.is_absolute() or not parts or ".." in parts:
        raise ValueError(f"not a relative path inside a capsule: {str(rel)!r}")
    return parts


def _is_link_at(name: str, dir_fd: int) -> bool:
    try:
        return stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    except OSError:
        return False


def _open_at(
    name: str, flags: int, dir_fd: int, rel: str, operation: str, error: type[CapsuleLinkError]
) -> int:
    try:
        return os.open(name, flags, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR) and _is_link_at(name, dir_fd):
            raise error(
                f"{rel} goes through a symlink ({name}); {operation} never follows one",
                links=(rel,),
            ) from exc
        raise


def _open_regular_fd(
    root: Path, rel: str | PurePosixPath, operation: str, error: type[CapsuleLinkError]
) -> int:
    parts = _split_rel(rel)
    rel_s = "/".join(parts)
    if os.open not in os.supports_dir_fd:  # pragma: no cover - non-POSIX fallback
        return _open_regular_fd_by_path(root, parts, rel_s, operation, error)
    dir_fd = os.open(root, _DIR_FLAGS)
    try:
        for part in parts[:-1]:
            next_fd = _open_at(part, _DIR_FLAGS | _NOFOLLOW, dir_fd, rel_s, operation, error)
            os.close(dir_fd)
            dir_fd = next_fd
        fd = _open_at(parts[-1], _FILE_FLAGS, dir_fd, rel_s, operation, error)
    finally:
        os.close(dir_fd)
    return _require_regular(fd, rel_s)


def _open_regular_fd_by_path(
    root: Path, parts: tuple[str, ...], rel: str, operation: str, error: type[CapsuleLinkError]
) -> int:  # pragma: no cover - only reached where openat(2) is unavailable
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise error(
                f"{rel} goes through a symlink ({part}); {operation} never follows one",
                links=(rel,),
            )
    return _require_regular(os.open(path, _FILE_FLAGS), rel)


def _require_regular(fd: int, rel: str) -> int:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise CapsuleSpecialFileError(f"{rel} is not a regular file")
    os.set_blocking(fd, True)
    return fd


def open_capsule_file(
    root: Path,
    rel: str | PurePosixPath,
    *,
    operation: str = "this operation",
    error: type[CapsuleLinkError] = CapsuleLinkError,
) -> BinaryIO:
    """Open capsule file *rel* for binary reading, following no link on the way.

    Raises:
        ValueError: *rel* is absolute, empty or holds ``..``.
        CapsuleLinkError: *rel* or one of its parent directories is a symlink
            (raised as *error*, a subclass the caller names).
        CapsuleSpecialFileError: *rel* is not a regular file.
        FileNotFoundError: *rel* does not exist.
    """
    return os.fdopen(_open_regular_fd(root, rel, operation, error), "rb")


def read_capsule_file(
    root: Path,
    rel: str | PurePosixPath,
    *,
    limit: int | None = None,
    operation: str = "this operation",
    error: type[CapsuleLinkError] = CapsuleLinkError,
) -> bytes:
    """Read capsule file *rel* through :func:`open_capsule_file`; at most *limit* bytes."""
    with open_capsule_file(root, rel, operation=operation, error=error) as handle:
        return handle.read() if limit is None else handle.read(limit)


@contextmanager
def _exclusive_writer(path: Path, mode: int) -> Iterator[BinaryIO]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC
    fd = os.open(path, flags, mode)
    with os.fdopen(fd, "wb") as out:
        yield out


def copy_capsule_tree(
    source: Path,
    dest: Path,
    *,
    operation: str,
    error: type[CapsuleLinkError] = CapsuleLinkError,
) -> CapsuleListing:
    """Copy capsule *source* to the new directory *dest*, never following a link.

    *dest* must not exist. Every file is read through :func:`open_capsule_file`,
    so a link planted after the listing is refused rather than copied. File mode
    bits and timestamps are kept, as ``shutil.copytree`` kept them. Special files
    are skipped. On any refusal the partial *dest* is removed and *error*, or
    :class:`CapsuleSpecialFileError`, is raised.
    """
    listing = list_capsule(source, operation=operation, error=error)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.mkdir(exist_ok=False)
    try:
        _copy_listing(listing, dest, operation, error)
        refuse_capsule_links(dest, operation=operation, capsule_name=source.name, error=error)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return listing


def _copy_listing(
    listing: CapsuleListing, dest: Path, operation: str, error: type[CapsuleLinkError]
) -> None:
    for rel in listing.dirs:
        (dest / rel).mkdir(parents=True, exist_ok=True)
    for rel in listing.files:
        with open_capsule_file(listing.root, rel, operation=operation, error=error) as src:
            st = os.fstat(src.fileno())
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with _exclusive_writer(target, stat.S_IMODE(st.st_mode)) as out:
                shutil.copyfileobj(src, out, _COPY_CHUNK)
            os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
