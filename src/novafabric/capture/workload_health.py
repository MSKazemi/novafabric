"""Carry the workload process's capture-loss counts into ``capture-health.json`` (M14).

Under ``nova capture`` the capture hooks run in the *workload's* Python process,
installed at interpreter start by the runner's ``sitecustomize`` loader
(:mod:`novafabric.runners._sitecustomize`). A fail-open drop there -- a record a
hook could not build or write -- is counted on that process's own
:class:`~novafabric.capture.event_recorder.EventRecorder`, which nothing in the
orchestrator can see. The orchestrator writes ``capture-health.json`` from its
own recorder, so before this module the workload's losses vanished silently.

The hand-off is two halves, both fail-open (ADR-0021):

* **workload side** -- :func:`register_workload_flush` (called by the loader)
  registers an ``atexit`` handler; at interpreter exit :func:`flush_workload_drops`
  writes the process recorder's drop counts to
  ``<capsule>/.capture-health.workload.<pid>.json``, only when something was
  dropped. One file per process, so a workload that starts more Python
  processes (each loads ``sitecustomize``) never has two writers on one file.
  The file is created ``O_CREAT | O_EXCL | O_NOFOLLOW``: a planted link or a
  stale file is never written through.
* **orchestrator side** -- :func:`merge_workload_drops` runs immediately before
  ``capture-health.json`` is written (and therefore before the residual pass and
  the evidence digests). It reads each hand-off file without following a link,
  bounded in count and size, validates it, adds the counts to the orchestrator's
  recorder under the ``workload`` source, and **removes** the file, so the
  hand-off is never left in the capsule as unexplained evidence.

Known limits: a process that ends with ``os._exit`` (or is killed) runs no
``atexit`` handler, so its drops are still lost; a process forked without
``exec`` inherits the parent's handler and counts, so the handler only writes
from the process that registered it (an inherited count is never written
twice).
"""

from __future__ import annotations

import atexit
import json
import logging
import os
from pathlib import Path
from typing import Final

from novafabric._capsule_fs import (
    CapsuleEntryError,
    open_capsule_file_for_write,
    read_capsule_file,
)
from novafabric.capture.event_recorder import EventRecorder, get_process_recorder

__all__ = [
    "WORKLOAD_HEALTH_PREFIX",
    "WORKLOAD_SOURCE",
    "flush_workload_drops",
    "merge_workload_drops",
    "register_workload_flush",
]

_log = logging.getLogger(__name__)

#: File-name prefix of a per-process hand-off file in the capsule root.
WORKLOAD_HEALTH_PREFIX: Final[str] = ".capture-health.workload."
_SUFFIX: Final[str] = ".json"

#: The ``capture-health.json`` ``sources`` key the merged counts are filed under.
WORKLOAD_SOURCE: Final[str] = "workload"

#: Bounds on what the orchestrator will read: hand-off files per capsule, bytes
#: per file, streams per file, characters per stream name, and one count.
MAX_HANDOFF_FILES: Final[int] = 1024
MAX_HANDOFF_BYTES: Final[int] = 64 * 1024
MAX_STREAMS: Final[int] = 64
MAX_STREAM_NAME: Final[int] = 128
MAX_COUNT: Final[int] = 2**53


class InvalidWorkloadHealthError(ValueError):
    """A hand-off file is not a well-formed ``{"dropped_events": {stream: n}}``."""


def _handoff_name(pid: int) -> str:
    return f"{WORKLOAD_HEALTH_PREFIX}{pid}{_SUFFIX}"


def flush_workload_drops(capsule_dir: Path, recorder: EventRecorder | None) -> Path | None:
    """Write *recorder*'s drop counts to this process's hand-off file.

    Returns the file written, or ``None`` when there was nothing to write or the
    write failed. Never raises: it runs at interpreter exit in the user's
    workload.
    """
    try:
        drops = recorder.drop_counts if recorder is not None else {}
        if not drops:
            return None
        name = _handoff_name(os.getpid())
        payload = json.dumps({"pid": os.getpid(), "dropped_events": drops}) + "\n"
        with open_capsule_file_for_write(
            capsule_dir, name, mode="x", operation="the capture-health hand-off"
        ) as handle:
            handle.write(payload.encode("utf-8"))
        return capsule_dir / name
    except Exception as exc:  # noqa: BLE001 -- fail-open at workload exit
        _log.warning(
            "capture_workload_health_not_written",
            extra={"capsule": str(capsule_dir), "error_type": type(exc).__name__},
        )
        return None


def _flush_at_exit(capsule_dir: Path, registered_pid: int) -> None:
    if os.getpid() != registered_pid:
        return  # a fork() child: its counts include the parent's; never double-count
    flush_workload_drops(capsule_dir, get_process_recorder())


def register_workload_flush(capsule_dir: Path) -> None:
    """Flush this process's drop counts into *capsule_dir* at interpreter exit."""
    atexit.register(_flush_at_exit, Path(capsule_dir), os.getpid())


def _validated_counts(raw: bytes) -> dict[str, int]:
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidWorkloadHealthError("not UTF-8 JSON") from exc
    counts = doc.get("dropped_events") if isinstance(doc, dict) else None
    if not isinstance(counts, dict) or len(counts) > MAX_STREAMS:
        raise InvalidWorkloadHealthError("dropped_events is not a bounded mapping")
    out: dict[str, int] = {}
    for stream, n in counts.items():
        if not isinstance(stream, str) or not stream or len(stream) > MAX_STREAM_NAME:
            raise InvalidWorkloadHealthError("invalid stream name")
        # bool is an int subclass; a JSON true is not a count.
        if type(n) is not int or not 0 <= n <= MAX_COUNT:
            raise InvalidWorkloadHealthError(f"invalid count for {stream!r}")
        if n:
            out[stream] = n
    return out


def _handoff_names(capsule_dir: Path) -> list[str]:
    names: list[str] = []
    try:
        with os.scandir(capsule_dir) as entries:
            for entry in entries:
                if entry.name.startswith(WORKLOAD_HEALTH_PREFIX) and entry.name.endswith(_SUFFIX):
                    names.append(entry.name)
    except OSError:
        return []
    names.sort()
    if len(names) > MAX_HANDOFF_FILES:
        _log.warning(
            "capture_workload_health_truncated",
            extra={"capsule": str(capsule_dir), "files": len(names), "read": MAX_HANDOFF_FILES},
        )
    return names


def _read_one(capsule_dir: Path, name: str) -> dict[str, int]:
    try:
        raw = read_capsule_file(
            capsule_dir, name, max_bytes=MAX_HANDOFF_BYTES,
            operation="the capture-health merge",
        )
        return _validated_counts(raw)
    except (OSError, CapsuleEntryError, InvalidWorkloadHealthError) as exc:
        _log.warning(
            "capture_workload_health_ignored",
            extra={"capsule": str(capsule_dir), "file": name, "reason": str(exc)[:200]},
        )
        return {}


def merge_workload_drops(capsule_dir: Path, recorder: EventRecorder) -> dict[str, int]:
    """Fold every workload hand-off file into *recorder*, then remove the files.

    Returns the merged workload counts (empty when there were none). Every
    hand-off file found is removed, a malformed or refused one included, so
    none is left in the capsule. Never raises.
    """
    merged: dict[str, int] = {}
    try:
        names = _handoff_names(capsule_dir)
        for index, name in enumerate(names):
            if index < MAX_HANDOFF_FILES:
                for stream, n in _read_one(capsule_dir, name).items():
                    merged[stream] = merged.get(stream, 0) + n
            try:
                os.unlink(capsule_dir / name)  # removes a link itself, never its target
            except OSError:
                pass
        if merged:
            recorder.merge_drops(merged, source=WORKLOAD_SOURCE)
    except Exception as exc:  # noqa: BLE001 -- capture health is fail-open
        _log.warning(
            "capture_workload_health_merge_failed",
            extra={"capsule": str(capsule_dir), "error_type": type(exc).__name__},
        )
    return merged
