"""Treat a capsule as untrusted input once the workload has run.

Under ``nova capture`` the workload writes into its own capsule directory, and
with ``--runner docker`` (or Kubernetes, via ``kubectl cp``) that directory is
mounted read-write into the container. Host-side finalisation then reads,
redacts and rewrites the capsule's files. If it followed a symbolic link the
workload planted at ``model-calls.jsonl``, the secret scanner would rewrite the
link's *target* -- a file outside the capsule, on the host, as the user running
``nova capture``: a container-to-host write. A FIFO planted at a stream would
hang the capture forever. A recording must never be redirected, corrupted or
hung by the thing it records.

:func:`inspect_capsule_after_workload` runs once, right after the runner
returns and before the orchestrator reads or writes any capsule path. It makes
two passes and never opens an entry it inspects:

1. For each *capsule-managed* path -- the streams, the files finalisation
   writes (``redaction_manifest.json`` included), ``outputs/stdout.txt`` /
   ``outputs/stderr.txt``, the ``inputs/`` and ``outputs/`` directories, and
   ``.seal/`` -- it ``lstat``\\ s the entry. An entry that is not what NovaFabric
   puts there (a symlink, FIFO, socket or device; a directory where a file
   belongs or the reverse; any ``.seal/`` before sealing) is removed, and the
   empty stream or directory NovaFabric needs is recreated.
2. It then walks the **whole** capsule tree, one ``openat(2)`` + ``O_NOFOLLOW``
   directory at a time, and removes every symlink, FIFO, socket and device it
   finds at any depth (``inputs/data.csv -> ~/.ssh/id_rsa``,
   ``outputs/media/blob.bin``, ``extensions/...``, a FIFO at ``inputs/pipe``).
   NovaFabric never writes any of them into a capsule, so each was planted.
   A directory nested deeper than :data:`MAX_SWEEP_DEPTH` is not descended and
   is recorded as ``not-swept``.

Each removal is recorded as a :class:`TamperFinding`; removing a link never
touches its target.

**Hard links are not tampering.** A regular file with ``st_nlink > 1`` is kept:
it is a regular file whose bytes the workload could read anyway (Linux's
``fs.protected_hardlinks``), and hard links are legitimate in workload output
(a virtualenv built with a hard-linking installer). What a hard link must never
do is carry a write out of the capsule, and it cannot: every host-side rewrite
replaces the entry by a rename and every append first gives the file a private
copy (:mod:`novafabric._capsule_fs`).

A finding is evidence, recorded three ways: ``capture-health.json``
``tampered_entries``; a ``<path>#tampered`` target in ``redaction-proof.json``
(``skipped``, ``skip_reason: "stream_tampered: ..."``); and the manifest's
``metadata.capture_tampered``, which ``nova verify`` and ``nova validate`` fail
on. Capture itself stays fail-open (ADR-0021): it exits with the workload's own
exit code and says on stderr that the capsule is tampered.

The sweep is the first line of defence, not the only one. Every later
host-side read and write of a capsule path goes through
:mod:`novafabric._capsule_fs` (no link followed, no FIFO opened blocking), so an
entry planted *after* the sweep -- by a process the workload left running, which
only the local runner allows -- is refused or replaced, never followed, and
never blocks.

The capsule directory itself is checked too: when the entry at the capsule path
is no longer the directory :class:`~novafabric.capture.capsule.CapsuleWriter`
created (the workload moved it and put a link in its place), the report says
``root_replaced`` and the orchestrator writes nothing more.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from novafabric._capsule_fs import entry_kind
from novafabric.capture.secrets import (
    ARTIFACT_SCAN_DIRS,
    ARTIFACT_SCAN_TARGETS,
    OTHER_FILE_KIND,
    SCAN_TARGETS,
    merge_scan_results,
    redact_secrets_in_text,
)
from novafabric.capture.streams import CAPSULE_STREAMS

__all__ = [
    "CAPSULE_STREAMS",
    "MAX_SWEEP_DEPTH",
    "TAMPER_METADATA_KEY",
    "TamperFinding",
    "TamperReport",
    "inspect_capsule_after_workload",
    "record_planted_entries_in_manifest",
    "record_tamper_in_manifest",
    "record_tamper_in_proof",
    "tamper_message",
]

_log = logging.getLogger(__name__)

#: The manifest ``metadata`` key that marks a capsule whose workload tampered
#: with capsule-managed paths. ``nova verify`` and ``nova validate`` fail on it.
TAMPER_METADATA_KEY: Final[str] = "capture_tampered"

#: The streams ``CapsuleWriter.open`` creates; recreated empty after a removal so
#: the capsule keeps the shape ``nova validate`` requires.
_REQUIRED_STREAMS: Final[frozenset[str]] = frozenset(
    {"model-calls.jsonl", "tool-calls.jsonl", "trace.jsonl", "assets.jsonl"}
)

#: Top-level files that must be regular files (or absent).
_MANAGED_FILES: Final[frozenset[str]] = frozenset(
    CAPSULE_STREAMS
    | {name for name, _kind in SCAN_TARGETS}
    | {name for name, _kind in ARTIFACT_SCAN_TARGETS}
    | {
        "replay.yaml",
        "redaction-proof.json",
        "capture-health.json",
        "c2pa-manifest.json",
        # Written by the ``--pii-subject`` capture stage (pii/capture_stage.py).
        "redaction_manifest.json",
    }
)

#: Directories that must be real directories; recreated empty after a removal.
_REQUIRED_DIRS: Final[tuple[str, ...]] = ("inputs", "outputs")

#: Files inside ``outputs/`` the orchestrator writes after the workload.
_MANAGED_OUTPUT_FILES: Final[tuple[str, ...]] = ("stdout.txt", "stderr.txt")

#: Optional directory under ``outputs/`` (ADR-0125 media blobs): real or absent.
_OPTIONAL_OUTPUT_DIRS: Final[tuple[str, ...]] = ("media",)

#: Does not exist until the seal is written; any entry here was planted.
_SEAL_DIR: Final[str] = ".seal"

#: Directories nested deeper than this are not descended by the tree sweep; each
#: is recorded as ``not-swept`` (NovaFabric writes nothing that deep).
MAX_SWEEP_DEPTH: Final[int] = 64

#: Findings recorded one by one; past this only the count is kept, so a workload
#: planting a million links cannot inflate the manifest or the proof.
_MAX_FINDINGS_RECORDED: Final[int] = 1000

#: Findings named in the one-line summary (``metadata.capture_tampered``).
_SUMMARY_SHOWN: Final[int] = 20

_MAX_LINK_TARGET: Final[int] = 256
_EMPTY_HASH: Final[str] = "sha256:" + hashlib.sha256(b"").hexdigest()
_DIR_FLAGS: Final[int] = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
)
_NOFOLLOW: Final[int] = getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True)
class TamperFinding:
    """One capsule-managed path the workload replaced with something else.

    ``kind`` is what was found (``symlink``, ``fifo``, ``socket``,
    ``char-device``, ``block-device``, ``directory``, ``regular-file``,
    ``unknown``); ``action`` what capture did (``removed``,
    ``removed-and-recreated``, ``not-removed``, ``not-swept``). ``link_target``
    is where a symlink pointed, redacted and truncated, never resolved.
    """

    path: str
    kind: str
    action: str
    link_target: str | None = None

    def as_dict(self) -> dict[str, str]:
        out = {"path": self.path, "kind": self.kind, "action": self.action}
        if self.link_target is not None:
            out["link_target"] = self.link_target
        return out


@dataclass(frozen=True)
class TamperReport:
    """What :func:`inspect_capsule_after_workload` found.

    ``findings`` holds at most ``_MAX_FINDINGS_RECORDED`` entries; ``omitted``
    counts the rest (removed all the same).
    """

    findings: tuple[TamperFinding, ...] = ()
    root_replaced: bool = False
    omitted: int = 0

    @property
    def tampered(self) -> bool:
        return self.root_replaced or bool(self.findings)

    @property
    def total(self) -> int:
        """How many entries were found, recorded or not."""
        return len(self.findings) + self.omitted

    def summary(self) -> str:
        """One line, e.g. ``model-calls.jsonl (symlink); tool-calls.jsonl (fifo)``."""
        if self.root_replaced:
            return "capsule directory replaced by the workload"
        shown = "; ".join(f"{f.path} ({f.kind})" for f in self.findings[:_SUMMARY_SHOWN])
        more = self.total - min(len(self.findings), _SUMMARY_SHOWN)
        return shown + (f"; and {more} more ({self.total} in total)" if more > 0 else "")

    def health_entries(self) -> list[dict[str, str]]:
        return [f.as_dict() for f in self.findings]


def _lstat_at(name: str, dir_fd: int) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _link_target(name: str, dir_fd: int) -> str | None:
    try:
        raw = os.readlink(name, dir_fd=dir_fd)
    except OSError:
        return None
    return redact_secrets_in_text(raw)[:_MAX_LINK_TARGET]


def _remove_at(name: str, dir_fd: int, mode: int) -> bool:
    """Remove entry *name* without following it. True when it is gone."""
    try:
        if stat.S_ISDIR(mode):
            # The fd-based rmtree never follows a link, and refuses to start at
            # one if the directory was swapped for a link after the lstat.
            shutil.rmtree(name, dir_fd=dir_fd)
        else:
            os.unlink(name, dir_fd=dir_fd)
        return True
    except OSError as exc:
        _log.warning(
            "capture_tampered_entry_not_removed",
            extra={"entry": name, "error_type": type(exc).__name__},
        )
        return False


def _recreate_at(name: str, dir_fd: int, *, directory: bool) -> bool:
    try:
        if directory:
            os.mkdir(name, dir_fd=dir_fd)
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW
            os.close(os.open(name, flags, 0o666, dir_fd=dir_fd))
        return True
    except OSError as exc:
        _log.warning(
            "capture_tampered_entry_not_recreated",
            extra={"entry": name, "error_type": type(exc).__name__},
        )
        return False


class _Sweep:
    """One pass over the managed paths below one directory fd."""

    def __init__(self) -> None:
        self.findings: list[TamperFinding] = []

    def check(
        self,
        name: str,
        rel: str,
        dir_fd: int,
        *,
        want_dir: bool,
        recreate: bool,
        forbidden: bool = False,
    ) -> None:
        st = _lstat_at(name, dir_fd)
        if st is None:
            if recreate:
                _recreate_at(name, dir_fd, directory=want_dir)
            return
        mode = st.st_mode
        if not forbidden:
            if want_dir and stat.S_ISDIR(mode):
                return
            if not want_dir and stat.S_ISREG(mode):
                return
        self.remove(name, rel, dir_fd, mode, recreate=recreate, want_dir=want_dir)

    def remove(
        self,
        name: str,
        rel: str,
        dir_fd: int,
        mode: int,
        *,
        recreate: bool = False,
        want_dir: bool = False,
    ) -> None:
        """Remove entry *name* (never followed), optionally recreate it, record it."""
        target = _link_target(name, dir_fd) if stat.S_ISLNK(mode) else None
        if not _remove_at(name, dir_fd, mode):
            action = "not-removed"
        elif recreate and _recreate_at(name, dir_fd, directory=want_dir):
            action = "removed-and-recreated"
        else:
            action = "removed"
        self.findings.append(TamperFinding(rel, entry_kind(mode), action, target))


def _open_dir_nofollow(name: str, dir_fd: int) -> int | None:
    try:
        return os.open(name, _DIR_FLAGS | _NOFOLLOW, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno not in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR):
            _log.warning(
                "capture_tamper_dir_unreadable",
                extra={"entry": name, "error_type": type(exc).__name__},
            )
        return None


def _open_rel_dir(root_fd: int, parts: tuple[str, ...]) -> int | None:
    """An fd on directory *parts* below *root_fd*, every component ``O_NOFOLLOW``."""
    fd = os.dup(root_fd)
    for part in parts:
        next_fd = _open_dir_nofollow(part, fd)
        os.close(fd)
        if next_fd is None:
            return None  # replaced by a link or removed since it was listed
        fd = next_fd
    return fd


def _sweep_tree(root_fd: int) -> list[TamperFinding]:
    """Remove every symlink, FIFO, socket and device anywhere below *root_fd*.

    Directories are re-opened from the root one ``O_NOFOLLOW`` component at a
    time instead of being held open, so a wide tree never exhausts descriptors.
    Nothing is opened but directories, so a FIFO or device never blocks.
    """
    sweep = _Sweep()
    pending: list[tuple[str, ...]] = [()]
    while pending:
        parts = pending.pop()
        fd = _open_rel_dir(root_fd, parts)
        if fd is None:
            continue
        try:
            try:
                names = sorted(os.listdir(fd))
            except OSError:
                continue
            for name in names:
                rel = "/".join((*parts, name))
                st = _lstat_at(name, fd)
                if st is None or stat.S_ISREG(st.st_mode):
                    continue  # a hard link is a regular file: kept (module docstring)
                if not stat.S_ISDIR(st.st_mode):
                    sweep.remove(name, rel, fd, st.st_mode)
                elif len(parts) + 1 >= MAX_SWEEP_DEPTH:
                    sweep.findings.append(TamperFinding(rel, "directory", "not-swept"))
                else:
                    pending.append((*parts, name))
        finally:
            os.close(fd)
    return sorted(sweep.findings, key=lambda f: f.path)


def _sweep_root(root_fd: int) -> list[TamperFinding]:
    sweep = _Sweep()
    for name in _REQUIRED_DIRS:
        sweep.check(name, name, root_fd, want_dir=True, recreate=True)
    sweep.check(_SEAL_DIR, _SEAL_DIR, root_fd, want_dir=True, recreate=False, forbidden=True)
    for name in sorted(_MANAGED_FILES):
        sweep.check(name, name, root_fd, want_dir=False, recreate=name in _REQUIRED_STREAMS)
    outputs_fd = _open_dir_nofollow("outputs", root_fd)
    if outputs_fd is not None:
        try:
            for name in _OPTIONAL_OUTPUT_DIRS:
                sweep.check(name, f"outputs/{name}", outputs_fd, want_dir=True, recreate=False)
            for name in _MANAGED_OUTPUT_FILES:
                sweep.check(name, f"outputs/{name}", outputs_fd, want_dir=False, recreate=False)
        finally:
            os.close(outputs_fd)
    return sweep.findings + _sweep_tree(root_fd)


def inspect_capsule_after_workload(
    capsule_dir: Path, *, expected_root: tuple[int, int] | None = None
) -> TamperReport:
    """Neutralise and report what the workload planted at capsule-managed paths.

    Call once, after the runner returns and before any other host-side read or
    write of *capsule_dir*. Checks the managed paths, then every entry of the
    tree (module docstring). *expected_root* is
    :attr:`~novafabric.capture.capsule.CapsuleWriter.root_identity`; when the
    directory now at *capsule_dir* is a different one (or a link), nothing is
    touched and the report says ``root_replaced``. Never follows a link, never
    opens a FIFO or device, never raises for what it finds.
    """
    try:
        st = os.lstat(capsule_dir)
    except OSError:
        return TamperReport(root_replaced=True)
    if not stat.S_ISDIR(st.st_mode) or (
        expected_root is not None and (st.st_dev, st.st_ino) != expected_root
    ):
        return TamperReport(root_replaced=True)
    try:
        root_fd = os.open(capsule_dir, _DIR_FLAGS | _NOFOLLOW)
    except OSError:
        return TamperReport(root_replaced=True)
    try:
        opened = os.fstat(root_fd)
        if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
            return TamperReport(root_replaced=True)  # swapped between lstat and open
        findings = _sweep_root(root_fd)
    finally:
        os.close(root_fd)
    if findings:
        _log.warning(
            "capture_tampered",
            extra={
                "capsule": str(capsule_dir),
                "count": len(findings),
                "paths": [f.path for f in findings],
            },
        )
    return TamperReport(
        findings=tuple(findings[:_MAX_FINDINGS_RECORDED]),
        omitted=max(0, len(findings) - _MAX_FINDINGS_RECORDED),
    )


def _proof_kind(path: str) -> str:
    known = dict(SCAN_TARGETS) | dict(ARTIFACT_SCAN_TARGETS)
    if path in known:
        return known[path]
    top = path.split("/", 1)[0]
    if "/" in path:
        return dict(ARTIFACT_SCAN_DIRS).get(top, OTHER_FILE_KIND)
    return OTHER_FILE_KIND


def record_tamper_in_proof(proof: dict[str, Any], report: TamperReport) -> dict[str, Any]:
    """Add one skipped ``<path>#tampered`` target per finding to *proof*; re-chained.

    The ``#`` suffix keeps the target apart from the target of the (recreated,
    empty) file at the same path, so no later scan replaces or folds it.
    """
    if not report.findings:
        return proof
    targets = [
        {
            "kind": _proof_kind(f.path),
            "ref": f"{f.path}#tampered",
            "bytes_scanned": 0,
            "findings_count": 0,
            "skipped": True,
            "binary": False,
            "hash_before_redaction": _EMPTY_HASH,
            "hash_after_redaction": _EMPTY_HASH,
            "skip_reason": (
                f"stream_tampered: {f.path} was a {f.kind} planted by the workload; "
                f"never followed or read ({f.action})"
            ),
        }
        for f in report.findings
    ]
    return merge_scan_results(proof, targets, [])


def record_tamper_in_manifest(manifest: dict[str, Any], report: TamperReport) -> None:
    """Set ``metadata.capture_tampered`` on *manifest* when the report has findings."""
    if not report.tampered:
        return
    metadata = dict(manifest.get("metadata") or {})
    metadata[TAMPER_METADATA_KEY] = report.summary()
    manifest["metadata"] = metadata


def record_planted_entries_in_manifest(
    manifest: dict[str, Any], entries: list[tuple[str, str]]
) -> None:
    """Add links / special files met at finalisation to ``metadata.capture_tampered``.

    *entries* are ``(rel, kind)`` pairs (:func:`novafabric._capsule_fs.
    find_capsule_special_entries`). They were planted after the post-workload
    sweep -- by a process the workload left running -- so finalisation never
    followed them; they are recorded in the manifest, which is the signed
    payload, so ``nova verify`` and ``nova validate`` fail the capsule.
    """
    if not entries:
        return
    shown = "; ".join(f"{rel} ({kind})" for rel, kind in entries[:_SUMMARY_SHOWN])
    more = len(entries) - _SUMMARY_SHOWN
    late = "planted after the sweep: " + shown + (f"; and {more} more" if more > 0 else "")
    metadata = dict(manifest.get("metadata") or {})
    previous = metadata.get(TAMPER_METADATA_KEY)
    metadata[TAMPER_METADATA_KEY] = f"{previous}; {late}" if previous else late
    manifest["metadata"] = metadata
    _log.warning(
        "capture_tampered_after_sweep",
        extra={"count": len(entries), "paths": [rel for rel, _ in entries[:_SUMMARY_SHOWN]]},
    )


def tamper_message(report: TamperReport) -> str:
    """The stderr line ``nova capture`` prints for a tampered capsule."""
    if report.root_replaced:
        return (
            "[novafabric] ✗ capture integrity: the capsule directory was replaced "
            "while the workload ran (moved, or swapped for a link). Nothing more was "
            "written to it; the capsule is incomplete and must not be trusted."
        )
    return (
        f"[novafabric] ✗ capture integrity: the workload replaced "
        f"{report.total} capsule path(s) with a link or special file: "
        f"{report.summary()}. None was followed; each was removed and is recorded "
        f"as tampered (metadata.{TAMPER_METADATA_KEY}, capture-health.json, "
        "redaction-proof.json). `nova verify` and `nova validate` fail this capsule."
    )
