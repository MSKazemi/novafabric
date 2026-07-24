"""fsync-durable atomic file/directory commit primitives.

The capsule writer advertises crash-safe, Lustre-safe atomic commit via
``os.rename``. A rename is atomic w.r.t. *visibility*, but on crash/power-loss
the renamed entry can be visible while the file *contents* (or the directory
entry itself) are not yet durable on disk. The fix is the standard
fsync-before-rename discipline used by the node spool
(``collector_cffi/spool.py``): fsync each file, fsync the containing directory,
rename, then fsync the parent directory so the rename itself is durable.

These helpers centralize that discipline so every atomic-commit site gets it
right the same way.
"""

from __future__ import annotations

import os
from pathlib import Path


def write_text_fsync(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` and fsync the file before returning."""
    with open(path, "w", encoding=encoding) as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def fsync_dir(path: Path) -> None:
    """fsync a directory so a rename/create within it is durable.

    Best-effort: some filesystems (and most non-POSIX platforms) do not permit
    opening a directory for fsync; a failure here does not undo the write.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_replace(tmp: Path, dest: Path) -> None:
    """Rename ``tmp`` → ``dest`` and fsync the parent dir so it is durable.

    The caller is responsible for having fsynced ``tmp``'s contents already
    (use :func:`write_text_fsync`). ``os.replace`` is used so an existing
    ``dest`` is overwritten atomically.
    """
    os.replace(str(tmp), str(dest))
    fsync_dir(dest.parent)
