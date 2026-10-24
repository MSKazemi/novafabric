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
* :func:`replace_capsule_file` rewrites one file by writing a fresh
  ``O_EXCL | O_NOFOLLOW`` temporary file beside it and renaming it into place,
  so a link, FIFO or hard link planted at the name is replaced, never written
  through. :func:`open_capsule_file_for_write` is the append-side twin (it
  refuses a link or special file instead). Capture finalisation writes through
  these two only: once the workload has run, the capsule is untrusted input.
* :func:`copy_capsule_tree` copies through :func:`open_capsule_file` and creates
  each destination file with ``O_CREAT | O_EXCL | O_NOFOLLOW``. It never calls
  ``shutil.copytree``, which re-resolves each path between its symlink check and
  the copy. It then checks the copy once more.

The capsule *root* may itself be reached through a link (``runs/latest``) when a
caller names it explicitly. It is resolved once, and only links *inside* it are
refused, as the evidence export has always done. A *scan* of many capsules
(:func:`iter_capsule_dirs`) is different: nobody named the entry, so a linked
entry is never listed. It is either an alias of a capsule the scan already
lists, or a way to pull a directory from elsewhere into the scan.

Readers that parse one well-known capsule file (``capsule.yaml``, a JSONL
stream, a facet) use one of three readers, all through
:func:`open_capsule_file`, so the rules above hold for them too:

* :func:`read_capsule_text` when the file must exist;
* :func:`read_required_capsule_text` (and :func:`read_required_capsule_member`)
  when an *absent* file means "no data" but a file that is **present and
  unusable** (a link, FIFO, device, directory, unreadable or over-size file)
  must stop the command. Every reader whose output claims to be complete or
  correct (an export, report, verdict, attestation, migration, lineage write)
  uses it for ``capsule.yaml``, ``capsule.json`` and the primary streams: such
  a file raises :class:`CapsuleRequiredFileError`, never an empty or partial
  result reported as success;
* :func:`read_capsule_text_or_none` only for best-effort listings (dashboards,
  search indexes), where an unusable file is logged and skipped.

**Hard links are read, never written through.** A hard link is an ordinary
directory entry for a regular file, so it is read and copied like any other
file. Under Linux's default ``fs.protected_hardlinks = 1`` a user can only
hard-link a file they own, or one they can already read and write, so a planted
hard link exposes nothing the planter could not read already. NovaFabric itself
never creates hard links in a capsule. Refusing every file with ``st_nlink > 1``
would break capsule trees kept under ``rsync --link-dest`` or ``cp -al``
snapshots (routine on HPC scratch filesystems) and workloads that build a
virtualenv in ``outputs/`` with a hard-linking installer, so hard links are
allowed and are not tampering. What a hard link must never do is carry a
*write* out of the capsule: :func:`replace_capsule_file` replaces the entry by a
rename, and :func:`open_capsule_file_for_write` gives a hard-linked file a
private copy first (copy-on-write) before it appends to or truncates it. This
is documented in ``docs/concepts.md`` ("Links inside a capsule are never
followed").

**Pinned capsule roots.** The root is opened by name, so a link the caller named
(``runs/latest``) keeps working. During capture finalisation that is not enough:
a process the workload left running could swap the capsule directory for a link
after the post-workload sweep. :func:`pin_capsule_root` records the directory's
``(st_dev, st_ino)`` for the current :func:`capsule_root_pin_scope`; every open
under a pinned root then opens it ``O_NOFOLLOW`` and raises
:class:`CapsuleRootChangedError` when it is no longer that directory.
"""

from __future__ import annotations

import errno
import functools
import logging
import os
import shutil
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Final, Literal, ParamSpec, TypeVar

from novafabric._safe_path import fsync_dir

__all__ = [
    "CapsuleEntryError",
    "CapsuleFileTooLargeError",
    "CapsuleLinkError",
    "CapsuleListing",
    "CapsuleRequiredFileError",
    "CapsuleRootChangedError",
    "CapsuleSpecialFileError",
    "capsule_root_pin_scope",
    "copy_capsule_tree",
    "entry_kind",
    "find_capsule_links",
    "find_capsule_special_entries",
    "is_capsule_regular_file",
    "iter_capsule_dirs",
    "list_capsule",
    "make_capsule_dir",
    "open_capsule_file",
    "open_capsule_file_for_write",
    "open_capsule_member_for_write",
    "pin_capsule_root",
    "read_capsule_file",
    "read_capsule_member_or_none",
    "read_capsule_text",
    "read_capsule_text_or_none",
    "read_required_capsule_file",
    "read_required_capsule_member",
    "read_required_capsule_text",
    "refuse_capsule_links",
    "replace_capsule_file",
    "with_capsule_root_pins",
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


class CapsuleFileTooLargeError(CapsuleEntryError):
    """A capsule file is larger than the reader's ``max_bytes`` bound."""


class CapsuleRootChangedError(CapsuleEntryError):
    """A pinned capsule root is no longer the directory that was pinned.

    Raised by every open under a root registered with :func:`pin_capsule_root`
    when the entry at that path is now a link, another directory, or gone: a
    process the workload left running moved the capsule after the sweep.
    Nothing is read from or written to whatever is there now.
    """


class CapsuleRequiredFileError(CapsuleEntryError):
    """A capsule file the command needs is present but cannot be used.

    Raised by :func:`read_required_capsule_text` for a link, FIFO, device,
    directory, unreadable or over-size file, or a path that leaves the capsule.
    The command stops instead of producing an empty or partial result. ``rel``
    is the capsule-relative path and ``reason`` the underlying refusal.
    """

    def __init__(self, message: str, *, rel: str, reason: str) -> None:
        super().__init__(message)
        self.rel = rel
        self.reason = reason


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


_P = ParamSpec("_P")
_R = TypeVar("_R")

#: Capsule roots pinned in the current scope: absolute path -> (st_dev, st_ino).
#: ``None`` outside :func:`capsule_root_pin_scope`, so nothing is ever pinned
#: process-wide.
_ROOT_PINS: ContextVar[dict[str, tuple[int, int]] | None] = ContextVar(
    "novafabric_capsule_root_pins", default=None
)


@contextmanager
def capsule_root_pin_scope() -> Iterator[None]:
    """A scope for :func:`pin_capsule_root`; every pin made inside ends with it."""
    token = _ROOT_PINS.set({})
    try:
        yield
    finally:
        _ROOT_PINS.reset(token)


def with_capsule_root_pins(fn: Callable[_P, _R]) -> Callable[_P, _R]:
    """Run *fn* inside its own :func:`capsule_root_pin_scope`."""

    @functools.wraps(fn)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with capsule_root_pin_scope():
            return fn(*args, **kwargs)

    return wrapper


def pin_capsule_root(root: Path, identity: tuple[int, int]) -> None:
    """Pin *root* to the directory with *identity* (``(st_dev, st_ino)``).

    Until the enclosing :func:`capsule_root_pin_scope` ends, every helper in this
    module that is handed *root* (or a directory below it) opens *root* with
    ``O_NOFOLLOW`` and raises :class:`CapsuleRootChangedError` if it is not that
    directory any more.

    Raises:
        RuntimeError: no :func:`capsule_root_pin_scope` is active.
    """
    pins = _ROOT_PINS.get()
    if pins is None:
        raise RuntimeError("pin_capsule_root() needs an active capsule_root_pin_scope()")
    pins[os.path.abspath(root)] = identity


def _pinned_root_for(root: Path) -> tuple[str, tuple[int, int], tuple[str, ...]] | None:
    """``(pinned base, identity, components below it)`` when *root* is under a pin."""
    pins = _ROOT_PINS.get()
    if not pins:
        return None
    key = os.path.abspath(root)
    for base, identity in pins.items():
        if key == base:
            return base, identity, ()
        prefix = base.rstrip(os.sep) + os.sep
        if key.startswith(prefix):
            return base, identity, tuple(p for p in key[len(prefix):].split(os.sep) if p)
    return None


def _open_root_fd(root: Path, operation: str, error: type[CapsuleLinkError]) -> int:
    """An fd on capsule directory *root*, checked against its pin when it has one."""
    pinned = _pinned_root_for(root)
    if pinned is None:
        return os.open(root, _DIR_FLAGS)
    base, identity, rest = pinned
    try:
        fd = os.open(base, _DIR_FLAGS | _NOFOLLOW)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
            raise CapsuleRootChangedError(
                f"capsule directory {base} was replaced or removed after it was "
                f"pinned; {operation} reads and writes nothing there"
            ) from exc
        raise
    try:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != identity:
            raise CapsuleRootChangedError(
                f"capsule directory {base} is no longer the directory capture "
                f"created; {operation} reads and writes nothing there"
            )
        rel_s = "/".join(rest)
        for part in rest:
            next_fd = _open_at(part, _DIR_FLAGS | _NOFOLLOW, fd, rel_s, operation, error)
            os.close(fd)
            fd = next_fd
    except BaseException:
        os.close(fd)
        raise
    return fd


def entry_kind(mode: int) -> str:
    """What an ``lstat`` mode is: ``symlink``, ``fifo``, ``socket``, ``char-device``,
    ``block-device``, ``directory``, ``regular-file`` or ``unknown``."""
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode):
        return "char-device"
    if stat.S_ISBLK(mode):
        return "block-device"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "regular-file"
    return "unknown"


def find_capsule_special_entries(root: Path) -> list[tuple[str, str]]:
    """Every symlink, FIFO, socket or device under *root* as sorted ``(rel, kind)``.

    Walks with ``os.walk(followlinks=False)`` and an ``lstat`` per entry; nothing
    is followed or opened, so it never blocks on a FIFO. NovaFabric never writes
    any of these into a capsule, so each one was planted. *root* itself may be a
    link the caller named. Hard links are regular files and are not listed.
    """
    found: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        for name in dirnames + filenames:
            path = here / name
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(mode) or stat.S_ISREG(mode):
                continue
            found.append((path.relative_to(root).as_posix(), entry_kind(mode)))
    return sorted(found)


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
    name: str,
    flags: int,
    dir_fd: int,
    rel: str,
    operation: str,
    error: type[CapsuleLinkError],
    *,
    perms: int = 0o777,
) -> int:
    try:
        return os.open(name, flags, perms, dir_fd=dir_fd)
    except OSError as exc:
        # EEXIST: an ``O_CREAT | O_EXCL`` open of a planted link (never followed).
        link_errnos = (errno.ELOOP, errno.EMLINK, errno.ENOTDIR, errno.EEXIST)
        if exc.errno in link_errnos and _is_link_at(name, dir_fd):
            raise error(
                f"{rel} goes through a symlink ({name}); {operation} never follows one",
                links=(rel,),
            ) from exc
        if exc.errno == errno.ENXIO:
            # A non-blocking write open of a FIFO with no reader, or any open of
            # a socket: never a regular file.
            raise CapsuleSpecialFileError(f"{rel} is not a regular file") from exc
        raise


def _open_regular_fd(
    root: Path, rel: str | PurePosixPath, operation: str, error: type[CapsuleLinkError]
) -> int:
    parts = _split_rel(rel)
    rel_s = "/".join(parts)
    if os.open not in os.supports_dir_fd:  # pragma: no cover - non-POSIX fallback
        return _open_regular_fd_by_path(root, parts, rel_s, operation, error)
    dir_fd = _parent_dir_fd(root, parts, rel_s, operation, error)
    try:
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
    max_bytes: int | None = None,
    operation: str = "this operation",
    error: type[CapsuleLinkError] = CapsuleLinkError,
) -> bytes:
    """Read capsule file *rel* through :func:`open_capsule_file`.

    *limit* truncates the read to that many bytes. *max_bytes* refuses a file
    larger than that with :class:`CapsuleFileTooLargeError` instead, for a
    reader whose answer would be wrong on a partial file.
    """
    with open_capsule_file(root, rel, operation=operation, error=error) as handle:
        if max_bytes is not None:
            data = handle.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise CapsuleFileTooLargeError(
                    f"{PurePosixPath(rel).as_posix()} is larger than {max_bytes} bytes"
                )
            return data if limit is None else data[:limit]
        return handle.read() if limit is None else handle.read(limit)


def read_capsule_text(
    root: Path,
    rel: str | PurePosixPath,
    *,
    operation: str = "this operation",
    max_bytes: int | None = None,
    errors: str = "strict",
) -> str:
    """Read capsule file *rel* as UTF-8 text, following no link on the way.

    The drop-in replacement for ``(root / rel).read_text()`` in a capsule
    reader. Raises what :func:`read_capsule_file` raises: ``FileNotFoundError``
    for a missing file, :class:`CapsuleLinkError` for a link,
    :class:`CapsuleSpecialFileError` for a FIFO or device, and
    :class:`CapsuleFileTooLargeError` above *max_bytes*.
    """
    raw = read_capsule_file(root, rel, operation=operation, max_bytes=max_bytes)
    return raw.decode("utf-8", errors=errors)


def read_capsule_text_or_none(
    root: Path,
    rel: str | PurePosixPath,
    *,
    operation: str = "this operation",
    max_bytes: int | None = None,
    errors: str = "replace",
) -> str | None:
    """Like :func:`read_capsule_text`, but ``None`` when the file cannot be used.

    For best-effort listings only (dashboards, indexes), where a missing file
    already means "no data": a missing file, a link, a special file or an
    over-size file all return ``None``. A reader whose output claims to be
    complete or correct uses :func:`read_required_capsule_text` instead. Every case
    but the missing file is logged as ``capsule_file_not_followed``, so a
    refusal is never silent. *root* itself must be a directory; an unreadable
    root (``OSError`` on open) is also ``None``.
    """
    try:
        return read_capsule_text(
            root, rel, operation=operation, max_bytes=max_bytes, errors=errors
        )
    except FileNotFoundError:
        return None
    except CapsuleEntryError as exc:
        _log.warning(
            "capsule_file_not_followed",
            extra={"capsule": str(root), "file": str(rel), "reason": str(exc)[:200]},
        )
        return None
    except (NotADirectoryError, IsADirectoryError, PermissionError) as exc:
        _log.warning(
            "capsule_file_unreadable",
            extra={"capsule": str(root), "file": str(rel), "reason": type(exc).__name__},
        )
        return None


def read_capsule_member_or_none(
    path: Path,
    *,
    operation: str = "this operation",
    max_bytes: int | None = None,
    errors: str = "replace",
) -> str | None:
    """:func:`read_capsule_text_or_none` for *path*, a file directly in a capsule root.

    The mechanical replacement for ``path.read_text()`` when *path* is
    ``<capsule>/<name>``: ``path.name`` is opened with ``O_NOFOLLOW`` relative
    to ``path.parent``, the capsule root (which, as everywhere, may itself be
    the link the caller named). For a file deeper in the capsule use
    :func:`read_capsule_text_or_none` with the root and the relative path, so
    every intermediate directory is checked too.
    """
    return read_capsule_text_or_none(
        path.parent, path.name, operation=operation, max_bytes=max_bytes, errors=errors
    )


#: ``open(2)`` flag per write mode of :func:`open_capsule_file_for_write`.
_WRITE_MODE_FLAGS: Final[dict[str, int]] = {
    "a": os.O_APPEND,
    "w": os.O_TRUNC,
    "x": os.O_EXCL,
}


def open_capsule_file_for_write(
    root: Path,
    rel: str | PurePosixPath,
    *,
    mode: Literal["a", "w", "x"] = "a",
    operation: str = "this operation",
    error: type[CapsuleLinkError] = CapsuleLinkError,
) -> BinaryIO:
    """Open capsule file *rel* for binary writing, following no link on the way.

    The write-side twin of :func:`open_capsule_file`, for code that writes into
    a capsule that already exists (an append to ``assets.jsonl`` after capture,
    a receipt written into a finished capsule). ``open(path, "a")`` follows a
    link planted at *path* and writes **through** it, to wherever it points;
    this never does. *mode* is ``"a"`` (append, create if missing), ``"w"``
    (truncate, create if missing) or ``"x"`` (create, refuse an existing
    entry). Parent directories are walked with ``openat(2)`` + ``O_NOFOLLOW``
    and must already exist; the file itself is opened ``O_NOFOLLOW |
    O_NONBLOCK`` and must be a regular file, so a FIFO is never written to (or
    blocked on). A new file gets the usual ``0o666 & ~umask`` permissions.

    Raises:
        ValueError: *rel* is absolute, empty or holds ``..``; or *mode* is unknown.
        CapsuleLinkError: *rel* or a parent directory is a symlink (as *error*).
        CapsuleSpecialFileError: *rel* exists and is not a regular file.
        FileExistsError: ``mode="x"`` and *rel* exists.
        FileNotFoundError: a parent directory does not exist.
    """
    try:
        mode_flag = _WRITE_MODE_FLAGS[mode]
    except KeyError:
        raise ValueError(f"unknown write mode {mode!r}; use 'a', 'w' or 'x'") from None
    parts = _split_rel(rel)
    rel_s = "/".join(parts)
    flags = (
        os.O_WRONLY | os.O_CREAT | mode_flag | _NOFOLLOW
        | getattr(os, "O_NONBLOCK", 0) | _CLOEXEC
    )
    if os.open not in os.supports_dir_fd:  # pragma: no cover - non-POSIX fallback
        path = root
        for part in parts:
            path = path / part
            if path.is_symlink():
                raise error(
                    f"{rel_s} goes through a symlink ({part}); {operation} never follows one",
                    links=(rel_s,),
                )
        fd = _require_regular(os.open(path, flags, 0o666), rel_s)
        return os.fdopen(fd, "ab" if mode == "a" else "wb")
    dir_fd = _parent_dir_fd(root, parts, rel_s, operation, error)
    try:
        # Never O_TRUNC at open time: a hard-linked file gets its private copy
        # before anything is truncated or appended.
        fd = _open_at(
            parts[-1], flags & ~os.O_TRUNC, dir_fd, rel_s, operation, error, perms=0o666
        )
        fd = _require_regular(fd, rel_s)
        try:
            if mode != "x" and os.fstat(fd).st_nlink > 1:
                fd = _break_hard_link(fd, parts[-1], dir_fd, copy=mode == "a")
            elif mode == "w":
                os.ftruncate(fd, 0)
        except BaseException:
            os.close(fd)
            raise
    finally:
        os.close(dir_fd)
    return os.fdopen(fd, "ab" if mode == "a" else "wb")


def _break_hard_link(fd: int, name: str, dir_fd: int, *, copy: bool) -> int:
    """Give hard-linked file *name* a private copy; return a write fd on the copy.

    The copy (the old bytes when *copy*, else empty; the same mode bits) is
    written to a fresh ``O_EXCL | O_NOFOLLOW`` temporary file and renamed over
    *name*, so the other names of the old inode -- possibly outside the capsule
    -- never see the write. *fd* is closed on success.
    """
    old = os.fstat(fd)
    tmp = f".{name}.nf-tmp-{os.urandom(6).hex()}"
    append = os.O_APPEND if copy else 0
    new_fd = os.open(
        tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC | append, 0o666,
        dir_fd=dir_fd,
    )
    try:
        os.fchmod(new_fd, stat.S_IMODE(old.st_mode))
        if copy:
            src = _require_regular(os.open(name, _FILE_FLAGS, dir_fd=dir_fd), name)
            try:
                now = os.fstat(src)
                if (now.st_dev, now.st_ino) != (old.st_dev, old.st_ino):
                    raise CapsuleSpecialFileError(f"{name} changed while it was being copied")
                while chunk := os.read(src, _COPY_CHUNK):
                    os.write(new_fd, chunk)
            finally:
                os.close(src)
            os.fsync(new_fd)  # the copy is durable before it replaces the original
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        with suppress(OSError):  # best effort, like _safe_path.fsync_dir
            os.fsync(dir_fd)
    except BaseException:
        os.close(new_fd)
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    os.close(fd)
    return new_fd


def open_capsule_member_for_write(
    path: Path,
    *,
    mode: Literal["a", "w", "x"] = "a",
    operation: str = "this operation",
    error: type[CapsuleLinkError] = CapsuleLinkError,
) -> BinaryIO:
    """:func:`open_capsule_file_for_write` for *path*, a file directly in a capsule root.

    The mechanical replacement for ``path.open("a")`` / ``path.write_text()``
    when *path* is ``<capsule>/<name>``. The capsule root, ``path.parent``, may
    itself be the link the caller named; only ``path.name`` is never followed.
    """
    return open_capsule_file_for_write(
        path.parent, path.name, mode=mode, operation=operation, error=error
    )


def _parent_dir_fd(
    root: Path, parts: tuple[str, ...], rel_s: str, operation: str, error: type[CapsuleLinkError]
) -> int:
    """An fd on the parent directory of *parts*, every component opened ``O_NOFOLLOW``.

    *root* itself is opened by name (it may be the link the caller named), unless
    it is pinned (:func:`pin_capsule_root`): then it must still be the pinned
    directory.
    """
    dir_fd = _open_root_fd(root, operation, error)
    try:
        for part in parts[:-1]:
            next_fd = _open_at(part, _DIR_FLAGS | _NOFOLLOW, dir_fd, rel_s, operation, error)
            os.close(dir_fd)
            dir_fd = next_fd
    except BaseException:
        os.close(dir_fd)
        raise
    return dir_fd


def replace_capsule_file(
    root: Path,
    rel: str | PurePosixPath,
    data: bytes,
    *,
    operation: str = "this operation",
    error: type[CapsuleLinkError] = CapsuleLinkError,
    file_mode: int | None = None,
) -> None:
    """Replace capsule file *rel* with *data* atomically, following no link.

    The write-side helper for a file whose whole content is (re)written
    (``capsule.yaml``, the redaction proof, a redacted stream). *data* goes to a
    new temporary file in the same directory, created ``O_CREAT | O_EXCL |
    O_NOFOLLOW``, which is then renamed over *rel*. ``rename(2)`` replaces the
    directory entry itself, so whatever was planted at *rel* (a symlink, a FIFO,
    a hard link to a file elsewhere) is replaced and never written through,
    opened or blocked on. Parent directories are walked with ``openat(2)`` +
    ``O_NOFOLLOW`` and must already exist. The new file gets ``0o666 & ~umask``,
    or *file_mode* when given (a redaction rewrite keeps the original's mode).

    Raises:
        ValueError: *rel* is absolute, empty or holds ``..``.
        CapsuleLinkError: a parent directory of *rel* is a symlink (as *error*).
        IsADirectoryError: *rel* is a directory.
        FileNotFoundError: a parent directory does not exist.
    """
    parts = _split_rel(rel)
    rel_s = "/".join(parts)
    name = parts[-1]
    if os.open not in os.supports_dir_fd:  # pragma: no cover - non-POSIX fallback
        parent = root.joinpath(*parts[:-1])
        tmp_path = parent / f".{name}.nf-tmp-{os.urandom(6).hex()}"
        fd = os.open(
            tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o666
        )
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp_path, parent / name)
        fsync_dir(parent)
        return
    dir_fd = _parent_dir_fd(root, parts, rel_s, operation, error)
    try:
        tmp = f".{name}.nf-tmp-{os.urandom(6).hex()}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC
        fd = os.open(tmp, flags, 0o666, dir_fd=dir_fd)
        try:
            with os.fdopen(fd, "wb") as out:
                if file_mode is not None:
                    os.fchmod(out.fileno(), file_mode)
                out.write(data)
                out.flush()
                os.fsync(out.fileno())  # durable before it becomes visible
            os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            with suppress(OSError):  # best effort, like _safe_path.fsync_dir
                os.fsync(dir_fd)  # ... and the rename itself survives a crash
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError:
                pass
            raise
    finally:
        os.close(dir_fd)


def make_capsule_dir(
    root: Path,
    rel: str | PurePosixPath,
    *,
    operation: str = "this operation",
    error: type[CapsuleLinkError] = CapsuleLinkError,
) -> None:
    """Create directory *rel* in the capsule if absent, following no link.

    The parent is walked with ``openat(2)`` + ``O_NOFOLLOW`` (and a pinned root
    is checked), and ``mkdirat(2)`` never follows a link at *rel*. An existing
    real directory is accepted; an existing link is refused with *error*; any
    other existing entry raises ``FileExistsError``.
    """
    parts = _split_rel(rel)
    rel_s = "/".join(parts)
    dir_fd = _parent_dir_fd(root, parts, rel_s, operation, error)
    try:
        try:
            os.mkdir(parts[-1], dir_fd=dir_fd)
        except FileExistsError:
            mode = os.stat(parts[-1], dir_fd=dir_fd, follow_symlinks=False).st_mode
            if stat.S_ISLNK(mode):
                raise error(
                    f"{rel_s} is a symlink; {operation} never follows one", links=(rel_s,)
                ) from None
            if not stat.S_ISDIR(mode):
                raise
    finally:
        os.close(dir_fd)


def _required_file_error(
    root: Path, rel_s: str, operation: str, exc: BaseException
) -> CapsuleRequiredFileError:
    reason = str(exc) or type(exc).__name__
    _log.warning(
        "capsule_required_file_unusable",
        extra={"capsule": str(root), "file": rel_s, "reason": reason[:200]},
    )
    return CapsuleRequiredFileError(
        f"{rel_s} in capsule {root} is present but cannot be used ({reason}); "
        f"{operation} refuses to produce a result without it. Replace it with "
        "a regular file or remove it, then try again.",
        rel=rel_s,
        reason=reason,
    )


def read_required_capsule_file(
    root: Path,
    rel: str | PurePosixPath,
    *,
    operation: str,
    max_bytes: int | None = None,
) -> bytes | None:
    """Capsule file *rel* as bytes; ``None`` only when it is **absent**.

    For a reader whose output claims to be complete or correct. An absent file
    (or capsule root) is "no data" and returns ``None``. A file that is present
    but cannot be used raises :class:`CapsuleRequiredFileError` naming it: a
    symbolic link (dangling or not), a FIFO, socket, device or directory, a
    file the process may not read, a file above *max_bytes*, or a *rel* that is
    absolute or holds ``..``. Such a file is never followed, read past or
    silently treated as empty.
    """
    rel_s = PurePosixPath(rel).as_posix()
    try:
        return read_capsule_file(root, rel, operation=operation, max_bytes=max_bytes)
    except FileNotFoundError:
        return None
    except (CapsuleEntryError, OSError, ValueError) as exc:
        raise _required_file_error(root, rel_s, operation, exc) from exc


def read_required_capsule_text(
    root: Path,
    rel: str | PurePosixPath,
    *,
    operation: str,
    max_bytes: int | None = None,
    errors: str = "replace",
) -> str | None:
    """:func:`read_required_capsule_file` decoded as UTF-8 (``errors`` as for ``bytes.decode``).

    ``None`` only when the file is absent; present but unusable raises
    :class:`CapsuleRequiredFileError`.
    """
    raw = read_required_capsule_file(root, rel, operation=operation, max_bytes=max_bytes)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8", errors=errors)
    except UnicodeDecodeError as exc:
        raise _required_file_error(root, PurePosixPath(rel).as_posix(), operation, exc) from exc


def read_required_capsule_member(
    path: Path,
    *,
    operation: str,
    max_bytes: int | None = None,
    errors: str = "replace",
) -> str | None:
    """:func:`read_required_capsule_text` for *path*, a file directly in a capsule root.

    The fail-closed counterpart of :func:`read_capsule_member_or_none`:
    ``path.name`` is opened relative to ``path.parent``.
    """
    return read_required_capsule_text(
        path.parent, path.name, operation=operation, max_bytes=max_bytes, errors=errors
    )


def is_capsule_regular_file(root: Path, rel: str | PurePosixPath) -> bool:
    """True when *rel* is a regular file under *root* with no link on its path.

    An ``lstat`` per component and nothing is opened, so it never blocks on a
    FIFO or touches a device. It is a filter, not a guard: read the file
    through :func:`open_capsule_file`, which checks again at open time.
    """
    try:
        parts = _split_rel(rel)
    except ValueError:
        return False
    path = root
    for i, part in enumerate(parts):
        path = path / part
        try:
            mode = path.lstat().st_mode
        except OSError:
            return False
        last = i == len(parts) - 1
        if not (stat.S_ISREG(mode) if last else stat.S_ISDIR(mode)):
            return False
    return True


def iter_capsule_dirs(
    base: Path, *, markers: tuple[str, ...] = ("capsule.yaml",)
) -> list[Path]:
    """The capsule directories directly under *base*, sorted, for a multi-capsule scan.

    An entry is listed when it is a real directory (``lstat``: a link to a
    directory is **not** listed) holding at least one of *markers* as a regular
    file (again no link). A linked entry is either an alias of a capsule this
    scan already lists (``latest -> 01J...``) or a directory pulled in from
    elsewhere, and a scan must not reach past *base*. *base* itself may be a
    link, as it is the directory the caller named.
    """
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return []
    out: list[Path] = []
    for entry in entries:
        try:
            if not stat.S_ISDIR(entry.lstat().st_mode):
                continue
        except OSError:
            continue
        if any(is_capsule_regular_file(entry, marker) for marker in markers):
            out.append(entry)
    return out


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
