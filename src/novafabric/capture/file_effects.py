"""Files the captured workload wrote, and their content when it ended (ADR-0321 amendment).

A mocked replay re-runs the workload. Model and tool calls are served from the
capsule, but a file the program itself appends to is appended to again: the
replay repeats the side effect (fidelity corpus S6). To let the fidelity verdict
see that, ``nova capture`` records **which files the workload's Python
process(es) opened for writing** and **the sha256 of each one after the
workload ended** in ``file-effects.json``. Replay digests the same paths after
the replayed process ends; a path whose content differs is a divergence.

Recorded: paths and digests only, never file contents. Three halves, all
fail-open towards the workload (ADR-0021) and fail-closed towards the verdict:

* **workload side** -- :func:`install_file_write_observer` (called by the
  ``sitecustomize`` loader) adds a :func:`sys.addaudithook` hook on CPython's
  ``open`` audit event (raised by ``open``/``io.open``/``FileIO`` and
  ``os.open``) and keeps the absolute path of every open with write intent
  (mode ``w``/``a``/``x``/``+``, or ``O_WRONLY``/``O_RDWR``/``O_CREAT``/
  ``O_APPEND``/``O_TRUNC``). It writes ``.file-effects.workload.<pid>.started``
  at install and ``.file-effects.workload.<pid>.json`` (the paths) at
  interpreter exit, so a process that ended without its exit hand-off
  (``os._exit``, a signal) is *seen* to be incomplete rather than read as
  "wrote nothing".
* **orchestrator side** -- :func:`collect_file_effects` reads and removes every
  hand-off file (bounded, never following a link), drops paths NovaFabric
  itself writes, digests each remaining path and returns the record
  :func:`render_record` serialises into the capsule.
* **replay side** -- :func:`install_replay_write_observer` (called by the
  replay dispatcher) runs the same hook in the replayed process and reports its
  writes through the replay event log (:func:`replay_written_paths` reads them
  back); :func:`parse_record`, :func:`resolve_recorded_path` and
  :func:`file_state` let ``replay/_fidelity.py`` digest the recorded paths the
  replay wrote again.

Known limits (documented, not hidden): only Python processes that load the
loader are observed -- a non-Python child process (``bash -c 'echo > f'``) or C
code that bypasses the audit events is not; an ``os.open(..., dir_fd=...)`` is
resolved against the working directory (NovaFabric's own capsule writes, which
use ``dir_fd``, are recognised by their caller and skipped); a ``fork()`` child that never execs
inherits the hook but not the hand-off; renames and deletes are not events, but
the end-of-run state of every written path is recorded, so a deleted output is
recorded as ``absent``. Digests are only computed for the local runner: under a
container or batch runner the paths name another filesystem, so no record is
written (replay then lists ``file_effects`` as unchecked).
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import stat
import sys
import sysconfig
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

from novafabric._capsule_fs import (
    CapsuleEntryError,
    capsule_root_pin_scope,
    open_capsule_file_for_write,
    pin_capsule_root,
    read_capsule_file,
)

__all__ = [
    "FILE_EFFECTS_REF",
    "FILE_EFFECTS_SCHEMA",
    "FileEffectsRecord",
    "FileState",
    "InvalidFileEffectsError",
    "collect_file_effects",
    "display_path",
    "file_state",
    "install_file_write_observer",
    "not_observed_record",
    "install_replay_write_observer",
    "parse_record",
    "replay_written_paths",
    "render_record",
    "resolve_recorded_path",
]

#: Capsule-relative path of the record.
FILE_EFFECTS_REF: Final[str] = "file-effects.json"
#: Record format identifier (bumped on an incompatible change).
FILE_EFFECTS_SCHEMA: Final[str] = "novafabric.file-effects/1"
#: What observed the writes.
OBSERVER: Final[str] = "python-open-audit-hook"

HANDOFF_PREFIX: Final[str] = ".file-effects.workload."
_STARTED_SUFFIX: Final[str] = ".started"
_DONE_SUFFIX: Final[str] = ".json"

#: Distinct written paths one process keeps; beyond it the record is incomplete.
MAX_PATHS_PER_PROCESS: Final[int] = 1024
#: Distinct written paths one capsule records; beyond it the record is incomplete.
MAX_PATHS: Final[int] = 4096
#: Longest path kept (characters); a longer one makes the record incomplete.
MAX_PATH_CHARS: Final[int] = 4096
#: Hand-off files read per capsule, and bytes per hand-off file.
MAX_HANDOFF_FILES: Final[int] = 1024
MAX_HANDOFF_BYTES: Final[int] = 8 * 1024 * 1024
#: Largest file digested; a larger one is recorded by size and is not comparable.
MAX_DIGEST_BYTES: Final[int] = 1024 * 1024 * 1024

#: File states. ``file`` carries a digest; ``too_large`` / ``unreadable`` cannot
#: be compared; ``absent`` and ``not_regular`` compare by state alone.
STATE_FILE: Final[str] = "file"
STATE_ABSENT: Final[str] = "absent"
STATE_NOT_REGULAR: Final[str] = "not_regular"
STATE_TOO_LARGE: Final[str] = "too_large"
STATE_UNREADABLE: Final[str] = "unreadable"
_STATES: Final[frozenset[str]] = frozenset(
    {STATE_FILE, STATE_ABSENT, STATE_NOT_REGULAR, STATE_TOO_LARGE, STATE_UNREADABLE}
)
UNCOMPARABLE_STATES: Final[frozenset[str]] = frozenset({STATE_TOO_LARGE, STATE_UNREADABLE})

_WRITE_MODE_CHARS: Final[frozenset[str]] = frozenset("wax+")
_WRITE_FLAGS: Final[int] = (
    os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
)
#: Pseudo filesystems: an open there is a device or kernel interface, not a file effect.
_PSEUDO_ROOTS: Final[tuple[str, ...]] = ("/dev", "/proc", "/sys")
#: NovaFabric's own package directory: an open called from code in it is the
#: capture writing its capsule (often ``os.open(name, dir_fd=...)``, whose audit
#: event carries the bare name only), never a workload effect.
_PACKAGE_DIR: Final[str] = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep
#: Frames looked through for the code that asked for the open (stdlib frames --
#: ``pathlib``, ``io`` wrappers -- are skipped; the first other frame decides).
_MAX_CALLER_FRAMES: Final[int] = 8


class InvalidFileEffectsError(ValueError):
    """A ``file-effects.json`` or a hand-off file is not well formed."""


# ── workload side ────────────────────────────────────────────────────────────


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def write_intent(mode: Any, flags: Any) -> bool:
    """Does an ``open`` audit event's ``(mode, flags)`` open the file for writing?"""
    if isinstance(mode, str) and _WRITE_MODE_CHARS.intersection(mode):
        return True
    return isinstance(flags, int) and not isinstance(flags, bool) and bool(flags & _WRITE_FLAGS)


def _stdlib_prefixes() -> tuple[str, ...]:
    paths = sysconfig.get_paths()
    return tuple(
        os.path.abspath(paths[k]).rstrip(os.sep) + os.sep
        for k in ("stdlib", "platstdlib") if paths.get(k)
    )


def _called_from_novafabric(frame: Any, stdlib: tuple[str, ...]) -> bool:
    """Is the first non-stdlib frame from *frame* outward NovaFabric's own code?"""
    for _ in range(_MAX_CALLER_FRAMES):
        if frame is None:
            return False
        name = frame.f_code.co_filename
        if name.startswith(_PACKAGE_DIR):
            return True
        if not (name.startswith(stdlib) or name.startswith("<")):
            return False  # the workload's own code (a tool body included)
        frame = frame.f_back
    return False


class FileWriteObserver:
    """Keep the absolute path of every file this process opens for writing.

    *skip_roots*: directories NovaFabric writes in (the capsule at capture, the
    replay event log at replay); opens under them are never kept.
    """

    def __init__(self, skip_roots: list[Path], *, cap: int = MAX_PATHS_PER_PROCESS) -> None:
        self._skip = tuple(os.path.abspath(r) for r in skip_roots)
        self._stdlib = _stdlib_prefixes()
        self._cap = cap
        self._paths: set[str] = set()
        #: Absolute paths already handled (kept or skipped): a repeated write to the
        #: same file -- and the second audit event of one ``open`` -- costs one lookup.
        self._handled: set[str] = set()
        self.capped = False
        self._pid = os.getpid()
        self._active = False
        self._lock = threading.Lock()
        self._busy = threading.local()

    @property
    def paths(self) -> list[str]:
        with self._lock:
            return sorted(self._paths)

    def install(self) -> bool:
        try:
            sys.addaudithook(self._hook)
        except Exception:  # noqa: BLE001 -- observation must never break the workload
            return False
        self._pid = os.getpid()
        self._active = True
        return True

    def stop(self) -> None:
        """Stop observing (an audit hook cannot be removed)."""
        self._active = False

    def _hook(self, event: str, args: tuple[Any, ...]) -> None:
        # Cheapest tests first: this runs for every audit event of the process.
        if event != "open" or not self._active or getattr(self._busy, "on", False):
            return
        self._busy.on = True
        try:
            if len(args) < 3 or not write_intent(args[1], args[2]):
                return
            raw = args[0]
            if isinstance(raw, os.PathLike):
                raw = os.fspath(raw)
            if raw in self._handled:
                return
            if not _called_from_novafabric(sys._getframe(1), self._stdlib):
                self._seen(args)
        except Exception:  # noqa: BLE001 -- observation must never break the workload
            pass
        finally:
            self._busy.on = False

    def _seen(self, args: tuple[Any, ...]) -> None:
        if len(args) < 3 or os.getpid() != self._pid:
            return
        path, mode, flags = args[0], args[1], args[2]
        if isinstance(path, int) or not write_intent(mode, flags):
            return
        decoded = os.fsdecode(path)
        text = os.path.abspath(decoded)
        if decoded == text and len(self._handled) < 4 * self._cap:
            self._handled.add(decoded)  # absolute: the same string means the same file
        if any(_under(text, r) for r in self._skip) or any(
            _under(text, r) for r in _PSEUDO_ROOTS
        ):
            return
        with self._lock:
            if text in self._paths:
                return
            if len(self._paths) >= self._cap or len(text) > MAX_PATH_CHARS:
                self.capped = True
                return
            self._paths.add(text)


def _write_handoff(capsule_dir: Path, name: str, payload: bytes) -> bool:
    try:
        with open_capsule_file_for_write(
            capsule_dir, name, mode="x", operation="the file-effects hand-off"
        ) as handle:
            handle.write(payload)
        return True
    except Exception:  # noqa: BLE001 -- fail-open in the workload
        return False


def _flush_at_exit(
    observer: FileWriteObserver, capsule_dir: Path, pid: int, identity: tuple[int, int]
) -> None:
    if os.getpid() != pid:
        return  # a fork() child inherited the handler; its parent hands off
    observer.stop()
    payload = json.dumps(
        {"pid": pid, "paths": observer.paths, "capped": observer.capped}
    ).encode("utf-8")
    try:
        # The capsule directory may have been replaced (by a link) since install:
        # write into the directory capture created, or nowhere.
        with capsule_root_pin_scope():
            pin_capsule_root(capsule_dir, identity)
            _write_handoff(capsule_dir, f"{HANDOFF_PREFIX}{pid}{_DONE_SUFFIX}", payload + b"\n")
    except Exception:  # noqa: BLE001 -- fail-open in the workload
        pass


def install_file_write_observer(capsule_dir: Path) -> FileWriteObserver | None:
    """Observe this process's file writes; hand them to the orchestrator at exit.

    Returns the observer, or ``None`` when it could not be installed. A process
    that is not observed writes no ``.started`` marker; when no process at all
    was observed the capsule gets no record, and replay reports its file
    effects as not compared (never as equal).
    """
    capsule_dir = Path(capsule_dir)
    pid = os.getpid()
    try:
        st = os.lstat(capsule_dir)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        return None  # a link or something else: write nothing through it
    identity = (st.st_dev, st.st_ino)
    if not _write_handoff(capsule_dir, f"{HANDOFF_PREFIX}{pid}{_STARTED_SUFFIX}", b""):
        return None
    observer = FileWriteObserver([capsule_dir])
    if not observer.install():
        return None
    # Registered from sitecustomize, before the workload's own handlers: atexit
    # runs last-registered first, so this one runs after them and sees their writes.
    atexit.register(_flush_at_exit, observer, capsule_dir, pid, identity)
    return observer


def _report_at_exit(
    observer: FileWriteObserver, emit: Callable[..., None], pid: int
) -> None:
    if os.getpid() != pid:
        return
    observer.stop()
    emit(REPLAY_WRITES_EVENT, paths=observer.paths, capped=observer.capped)


#: Replay event-log events (``replay/_contract.py:ReplayEventLog``).
REPLAY_OBSERVER_EVENT: Final[str] = "file_observer_installed"
REPLAY_WRITES_EVENT: Final[str] = "files_written"


def install_replay_write_observer(
    emit: Callable[..., None], *, skip_roots: list[Path]
) -> FileWriteObserver | None:
    """The same observer in a replayed process: it reports through *emit*
    (``file_observer_installed`` now, ``files_written`` at interpreter exit), so
    replay can tell the recorded files the replay wrote again from those it
    never touched."""
    observer = FileWriteObserver(skip_roots)
    if not observer.install():
        return None
    emit(REPLAY_OBSERVER_EVENT)
    atexit.register(_report_at_exit, observer, emit, os.getpid())
    return observer


def replay_written_paths(events: list[dict[str, Any]]) -> tuple[set[str] | None, str | None]:
    """The absolute paths the replayed processes wrote, from the replay event log,
    or ``(None, why they are not known)``: no observer, a process that ended without
    reporting (``os._exit``), or a cap hit."""
    installed: set[Any] = set()
    reported: set[Any] = set()
    paths: set[str] = set()
    for ev in events:
        kind = ev.get("event")
        if kind == REPLAY_OBSERVER_EVENT:
            installed.add(ev.get("pid"))
        elif kind == REPLAY_WRITES_EVENT:
            listed = ev.get("paths")
            if ev.get("capped") is not False or not isinstance(listed, list):
                return None, "a replayed process wrote more files than the observer keeps"
            reported.add(ev.get("pid"))
            paths.update(p for p in listed if isinstance(p, str))
    if not installed:
        return None, "the replayed process's file writes were not observed"
    if installed - reported:
        return None, (
            "a replayed process ended without reporting its file writes (os._exit or a signal)"
        )
    return paths, None


# ── file state (both sides) ──────────────────────────────────────────────────


@dataclass(frozen=True)
class FileState:
    """One path's state at the end of a run."""

    path: str  # display form: see :func:`display_path`
    state: str
    sha256: str | None = None
    size_bytes: int | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"path": self.path, "state": self.state}
        if self.sha256 is not None:
            out["sha256"] = self.sha256
        if self.size_bytes is not None:
            out["size_bytes"] = self.size_bytes
        return out

    def describe(self) -> str:
        """Short form for a fidelity difference: a digest or a state name."""
        if self.state == STATE_FILE and self.sha256:
            return self.sha256
        if self.state == STATE_TOO_LARGE and self.size_bytes is not None:
            return f"too_large({self.size_bytes} bytes)"
        return self.state


def file_state(path: Path, display: str, *, max_bytes: int = MAX_DIGEST_BYTES) -> FileState:
    """The state of *path* now. Opened non-blocking (a FIFO is never waited on)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return FileState(display, STATE_ABSENT)
    except IsADirectoryError:
        return FileState(display, STATE_NOT_REGULAR)
    except OSError:
        try:
            exists = os.path.lexists(path)
        except OSError:
            exists = True
        return FileState(display, STATE_UNREADABLE if exists else STATE_ABSENT)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return FileState(display, STATE_NOT_REGULAR)
        if info.st_size > max_bytes:
            return FileState(display, STATE_TOO_LARGE, size_bytes=info.st_size)
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "rb", closefd=True) as fh:
            fd = -1
            while chunk := fh.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    return FileState(display, STATE_TOO_LARGE, size_bytes=size)
                digest.update(chunk)
        return FileState(display, STATE_FILE, "sha256:" + digest.hexdigest(), size)
    except OSError:
        return FileState(display, STATE_UNREADABLE)
    finally:
        if fd >= 0:
            os.close(fd)


def display_path(absolute: str, *, cwd: Path, home: Path) -> str:
    """How a written path is stored: relative to the working directory when under
    it, ``~/...`` when under the home directory, else absolute (the manifest's
    ``working_directory`` convention). Replay resolves it the same way."""
    pure = PurePosixPath(absolute)
    for base, prefix in ((cwd, ""), (home, "~/")):
        try:
            rel = pure.relative_to(PurePosixPath(os.path.abspath(base)))
        except ValueError:
            continue
        if str(rel) != ".":
            return prefix + rel.as_posix()
    return pure.as_posix()


def resolve_recorded_path(display: str, *, cwd: Path, home: Path) -> Path:
    """The inverse of :func:`display_path` against this run's directories."""
    if display.startswith("~/"):
        return home / display[2:]
    if display.startswith("/"):
        return Path(display)
    return cwd / display


# ── orchestrator side ────────────────────────────────────────────────────────


@dataclass
class FileEffectsRecord:
    """The content of ``file-effects.json``."""

    complete: bool
    files: list[FileState]
    reason: str | None = None


def render_record(record: FileEffectsRecord) -> str:
    doc: dict[str, Any] = {
        "schema": FILE_EFFECTS_SCHEMA,
        "observer": OBSERVER,
        "complete": record.complete,
        "files": [f.as_dict() for f in record.files],
    }
    if record.reason:
        doc["reason"] = record.reason
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def not_observed_record(reason: str) -> FileEffectsRecord:
    return FileEffectsRecord(complete=False, files=[], reason=reason)


def _handoff_names(capsule_dir: Path) -> list[str]:
    try:
        with os.scandir(capsule_dir) as entries:
            return sorted(e.name for e in entries if e.name.startswith(HANDOFF_PREFIX))
    except OSError:
        return []


def _validated_paths(raw: bytes) -> tuple[list[str], bool]:
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidFileEffectsError("hand-off is not UTF-8 JSON") from exc
    paths = doc.get("paths") if isinstance(doc, dict) else None
    capped = doc.get("capped") if isinstance(doc, dict) else None
    if not isinstance(paths, list) or not isinstance(capped, bool):
        raise InvalidFileEffectsError("hand-off has no paths list")
    if len(paths) > MAX_PATHS_PER_PROCESS:
        raise InvalidFileEffectsError("hand-off lists too many paths")
    for p in paths:
        if not isinstance(p, str) or not p.startswith("/") or len(p) > MAX_PATH_CHARS:
            raise InvalidFileEffectsError("hand-off path is not an absolute path")
    return paths, capped


def _excluded_roots(capsule_dir: Path, extra: list[Path]) -> list[str]:
    roots = [os.path.abspath(capsule_dir), os.path.realpath(capsule_dir), *_PSEUDO_ROOTS]
    for p in extra:
        roots += [os.path.abspath(p), os.path.realpath(p)]
    return roots


def _is_excluded(path: str, roots: list[str]) -> bool:
    if "__pycache__" in PurePosixPath(path).parts or path.endswith((".pyc", ".pyo")):
        return True  # the interpreter's bytecode cache, not the program's output
    return any(_under(path, r) for r in roots)


def collect_file_effects(
    capsule_dir: Path,
    *,
    cwd: Path,
    home: Path,
    exclude: list[Path],
    digest: bool,
) -> FileEffectsRecord | None:
    """Read and remove every hand-off file; return the record, or ``None`` when no
    process was observed (a non-Python workload, or the loader did not run).

    *exclude*: roots NovaFabric itself writes under (its home, the audit log).
    *digest*: False under a runner whose paths name another filesystem; the
    hand-offs are still removed, and no record is returned (nothing recorded is
    not an inconsistency: replay lists the check as unchecked).
    """
    names = _handoff_names(capsule_dir)
    started: set[str] = set()
    done: dict[str, bytes | None] = {}
    for index, name in enumerate(names):
        stem = name[len(HANDOFF_PREFIX):]
        if index < MAX_HANDOFF_FILES:
            if stem.endswith(_STARTED_SUFFIX):
                started.add(stem[: -len(_STARTED_SUFFIX)])
            elif stem.endswith(_DONE_SUFFIX):
                try:
                    done[stem[: -len(_DONE_SUFFIX)]] = read_capsule_file(
                        capsule_dir, name, max_bytes=MAX_HANDOFF_BYTES,
                        operation="the file-effects merge",
                    )
                except (OSError, CapsuleEntryError):
                    done[stem[: -len(_DONE_SUFFIX)]] = None
        try:
            os.unlink(capsule_dir / name)  # removes a link itself, never its target
        except OSError:
            pass
    if not started and not done or not digest:
        return None
    problems: list[str] = []
    if len(names) > MAX_HANDOFF_FILES:
        problems.append(f"more than {MAX_HANDOFF_FILES} hand-off files")
    missing = sorted(started - set(done))
    if missing:
        problems.append(
            f"{len(missing)} observed process(es) ended without handing off their "
            "writes (os._exit or a signal)"
        )
    paths: set[str] = set()
    for pid in sorted(done):
        raw = done[pid]
        try:
            if raw is None:
                raise InvalidFileEffectsError("hand-off unreadable")
            listed, capped = _validated_paths(raw)
        except InvalidFileEffectsError as exc:
            problems.append(f"process {pid}: {exc}")
            continue
        if capped:
            problems.append(f"process {pid} wrote more than {MAX_PATHS_PER_PROCESS} files")
        paths.update(listed)
    roots = _excluded_roots(capsule_dir, exclude)
    kept = sorted(p for p in paths if not _is_excluded(p, roots))
    if len(kept) > MAX_PATHS:
        problems.append(f"more than {MAX_PATHS} files written")
        kept = kept[:MAX_PATHS]
    files = [file_state(Path(p), display_path(p, cwd=cwd, home=home)) for p in kept]
    by_display: dict[str, FileState] = {}
    for f in files:
        by_display.setdefault(f.path, f)
    return FileEffectsRecord(
        complete=not problems,
        files=sorted(by_display.values(), key=lambda f: f.path),
        reason="; ".join(problems) or None,
    )


# ── replay side ──────────────────────────────────────────────────────────────


def parse_record(raw: bytes) -> FileEffectsRecord:
    """``file-effects.json`` bytes as a record; raises :class:`InvalidFileEffectsError`."""
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidFileEffectsError("file-effects.json is not UTF-8 JSON") from exc
    if not isinstance(doc, dict) or doc.get("schema") != FILE_EFFECTS_SCHEMA:
        raise InvalidFileEffectsError("file-effects.json has an unknown schema")
    complete, files, reason = doc.get("complete"), doc.get("files"), doc.get("reason")
    if not isinstance(complete, bool) or not isinstance(files, list):
        raise InvalidFileEffectsError("file-effects.json lacks complete/files")
    if len(files) > MAX_PATHS:
        raise InvalidFileEffectsError("file-effects.json lists too many files")
    out: list[FileState] = []
    for entry in files:
        if not isinstance(entry, dict):
            raise InvalidFileEffectsError("a files entry is not an object")
        path, state = entry.get("path"), entry.get("state")
        sha, size = entry.get("sha256"), entry.get("size_bytes")
        if not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS:
            raise InvalidFileEffectsError("a files entry has no path")
        if state not in _STATES:
            raise InvalidFileEffectsError(f"unknown file state {state!r}")
        if state == STATE_FILE and not (isinstance(sha, str) and sha.startswith("sha256:")):
            raise InvalidFileEffectsError(f"{path}: a file entry without a sha256")
        if size is not None and (type(size) is not int or size < 0):
            raise InvalidFileEffectsError(f"{path}: invalid size_bytes")
        out.append(FileState(path, state, sha if isinstance(sha, str) else None, size))
    return FileEffectsRecord(
        complete=complete, files=out, reason=reason if isinstance(reason, str) else None
    )
