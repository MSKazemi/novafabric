"""Record ``fsync`` and rename calls, to assert that a write survives a crash.

A rename is atomic for *visibility* only. After a power loss the new name can
point at a file whose bytes never reached the disk, or the rename itself can be
lost, unless the writer:

1. fsyncs the temporary file **before** renaming it over the target, and
2. fsyncs the target's directory **after** the rename.

ext4's rename heuristic usually hides a missing step; XFS and Lustre (both
deployment targets) do not. A zero-length ``holds.jsonl`` reads as "no holds",
so for a legal-hold ledger the missing step is a fail-open deletion gate.

:class:`DurabilityRecorder` wraps ``os.fsync``, ``os.replace`` and ``os.rename``
(``Path.rename``/``Path.replace`` and ``shutil.move`` go through them) and keeps
the order of events, with every path resolved through ``/proc/self/fd`` so a
write made with ``dir_fd`` is recorded under its absolute path. Linux only; the
fixture skips elsewhere.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

_PROC_FD = Path("/proc/self/fd")


@dataclass(frozen=True)
class Event:
    kind: str  # "fsync_file" | "fsync_dir" | "rename"
    path: str
    dest: str | None = None


def _fd_path(fd: int) -> str:
    return os.readlink(_PROC_FD / str(fd))


def _abs(path: Any, dir_fd: int | None) -> str:
    raw = os.fsdecode(path)
    if dir_fd is not None and not os.path.isabs(raw):
        return os.path.join(_fd_path(dir_fd), raw)
    return os.path.abspath(raw)


class DurabilityRecorder:
    """Monkeypatches the three calls and records what they did, in order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        if not _PROC_FD.is_dir():
            pytest.skip("needs /proc/self/fd to resolve file descriptors")
        self.events: list[Event] = []
        real_fsync, real_replace, real_rename = os.fsync, os.replace, os.rename

        def fsync(fd: int) -> None:
            kind = "fsync_dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync_file"
            self.events.append(Event(kind, _fd_path(fd)))
            real_fsync(fd)

        def _rename_wrapper(real: Any) -> Any:
            def rename(
                src: Any, dst: Any, *, src_dir_fd: int | None = None, dst_dir_fd: int | None = None
            ) -> None:
                self.events.append(
                    Event("rename", _abs(src, src_dir_fd), _abs(dst, dst_dir_fd))
                )
                real(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

            return rename

        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(os, "replace", _rename_wrapper(real_replace))
        monkeypatch.setattr(os, "rename", _rename_wrapper(real_rename))

    def clear(self) -> None:
        self.events.clear()

    def assert_durable(self, target: Path) -> None:
        """*target* was renamed into place from a fsynced file, then its directory fsynced."""
        dest = os.path.realpath(target)
        renames = [
            i for i, e in enumerate(self.events) if e.kind == "rename" and e.dest == dest
        ]
        assert renames, f"{dest} was never renamed into place; events: {self.events}"
        last = renames[-1]
        src = self.events[last].path
        assert any(
            e.kind == "fsync_file" and e.path == src for e in self.events[:last]
        ), f"{src} was renamed to {dest} without being fsynced first; events: {self.events}"
        parent = os.path.dirname(dest)
        assert any(
            e.kind == "fsync_dir" and e.path == parent for e in self.events[last + 1 :]
        ), f"{parent} was not fsynced after the rename to {dest}; events: {self.events}"
