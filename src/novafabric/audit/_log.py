from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import threading
from collections.abc import Collection, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._models import AuditEntry, AuditEventType

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX: in-process lock only
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

#: Bytes read per step when scanning the log backwards for the last entry.
_TAIL_BLOCK = 64 * 1024

#: How often ``append`` retries when the file it locked was replaced meanwhile.
_MAX_REOPEN = 8

# One in-process lock per log path. ``flock`` is held per open file description, so
# two threads opening the same log each get their own and would not exclude each
# other on every platform; this lock serialises them first.
_PATH_LOCKS: dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


class AuditLogLockError(OSError):
    """The audit log kept being replaced while ``append`` tried to lock it."""


def _path_lock(path: Path) -> threading.Lock:
    key = os.path.abspath(path)
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = _PATH_LOCKS[key] = threading.Lock()
        return lock


def _flock(fd: int, exclusive: bool) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)


def _funlock(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _read_block(fd: int, offset: int, length: int) -> bytes:
    """``pread`` that tolerates short reads (one seam, so tests can count bytes)."""
    chunks: list[bytes] = []
    while length > 0:
        data = os.pread(fd, length, offset)
        if not data:
            break
        chunks.append(data)
        offset += len(data)
        length -= len(data)
    return b"".join(chunks)


def _entry_hash_of(line: bytes) -> tuple[bool, str | None]:
    """``(is_entry, entry_hash)`` for one log line; corrupt lines are not entries."""
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        log.warning("audit log: skipping corrupt line while finding the tail: %r", line[:200])
        return False, None
    if not isinstance(d, dict):
        log.warning("audit log: skipping non-object line while finding the tail")
        return False, None
    value = d.get("entry_hash")
    return True, (value if isinstance(value, str) and value else None)


def _last_entry_hash(fd: int, size: int) -> str | None:
    """The ``entry_hash`` of the last well-formed line, reading backwards from *size*.

    Reads ``_TAIL_BLOCK`` bytes at a time, so a healthy log costs one block however
    long it is. Corrupt lines are skipped, exactly as the old full replay did; only a
    run of corrupt lines reaching back to the start reads the whole file.
    """
    end = size
    carry = b""
    while end > 0:
        start = max(0, end - _TAIL_BLOCK)
        buf = _read_block(fd, start, end - start) + carry
        lines = buf.split(b"\n")
        # lines[0] may continue in the previous block unless we reached offset 0.
        carry = lines.pop(0) if start > 0 else b""
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            is_entry, entry_hash = _entry_hash_of(line)
            if is_entry:
                return entry_hash
        end = start
    return None


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _canonical_json(d: dict[str, Any]) -> str:
    # Sorted keys + no whitespace so the hash input is deterministic across
    # Python dict orderings and serialisers.
    return json.dumps(d, separators=(",", ":"), sort_keys=True)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _entry_to_dict(entry: AuditEntry) -> dict[str, Any]:
    # model_dump_json() is used instead of model_dump(mode='json') because
    # Pydantic's JSON serialiser correctly renders datetime as an ISO-8601
    # string without requiring manual field-by-field handling; round-tripping
    # through json.loads gives us plain JSON-native types for canonical hashing.
    d: dict[str, Any] = json.loads(entry.model_dump_json())
    return d


def _compute_entry_hash(entry: AuditEntry) -> str:
    # entry_hash cannot include itself, so strip it before hashing.
    d = _entry_to_dict(entry)
    d.pop("entry_hash", None)
    return _sha256(_canonical_json(d))


class AuditLog:
    """Append-only, SHA-256 hash-chained audit log stored as JSONL.

    Each line is a JSON-serialized :class:`AuditEntry`.  The ``entry_hash``
    field is the SHA-256 of the canonical JSON of the entry **without**
    ``entry_hash``.  The ``prev_hash`` field is the ``entry_hash`` of the
    immediately preceding entry, providing a tamper-evident chain.

    Guarantee: detects accidental corruption and naive tampering (e.g. editing
    a field without recomputing hashes).  This is an *unkeyed* hash chain —
    an adversary with file-write access and the ability to compute SHA-256 can
    rewrite the chain undetected.  Cryptographic signing with a secret key is
    addressed by NovaSeal (ADR-0030); that layer is separate from this one.

    Concurrency: :meth:`append` is atomic across threads and processes. It takes a
    per-path in-process lock and an exclusive ``fcntl.flock`` on the log file,
    reads the last ``entry_hash`` from the file's tail (bounded, not a replay),
    appends one line and fsyncs before releasing. Every instance re-reads the
    tail, so a long-lived instance never chains onto a stale predecessor.
    :meth:`verify` and :meth:`query` hold a shared lock. ``flock`` is advisory and
    is not reliable on every network filesystem (NFS without lock support, some
    Lustre mounts without ``-o flock``); a writer that ignores the lock is not
    excluded.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        # The hash this instance last wrote. Informational only: ``append`` always
        # re-reads the tail under the lock, because another instance, thread or
        # process may have appended since.
        self._last_hash: str | None = None

    @contextlib.contextmanager
    def _locked_for_append(self) -> Iterator[int]:
        """Yield an fd open for append, holding the in-process lock and ``LOCK_EX``.

        Retries if the path was replaced (e.g. a restore) between ``open`` and
        ``flock``, so the entry never lands in an unlinked inode.
        """
        with _path_lock(self._path):
            for _ in range(_MAX_REOPEN):
                fd = os.open(self._path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o666)
                try:
                    _flock(fd, exclusive=True)
                    try:
                        same = os.path.samestat(os.fstat(fd), os.stat(self._path))
                    except FileNotFoundError:
                        same = False
                    if same:
                        try:
                            yield fd
                        finally:
                            _funlock(fd)
                        return
                    _funlock(fd)
                finally:
                    os.close(fd)
            raise AuditLogLockError(f"audit log kept being replaced: {self._path}")

    @contextlib.contextmanager
    def _locked_for_read(self) -> Iterator[None]:
        """Hold ``LOCK_SH`` so a reader never sees a half-written last line."""
        try:
            fd = os.open(self._path, os.O_RDONLY)
        except FileNotFoundError:
            yield
            return
        try:
            _flock(fd, exclusive=False)
            try:
                yield
            finally:
                _funlock(fd)
        finally:
            os.close(fd)

    def append(
        self,
        event_type: AuditEventType,
        actor: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
    ) -> AuditEntry:
        """Append one entry to the log and return the finalised :class:`AuditEntry`."""
        # Read-last-hash, write and fsync are one critical section across threads
        # and processes; without it two writers chain onto the same predecessor and
        # fork the chain (review r8: 12 prev_hash mismatches in 40 bypasses).
        with self._locked_for_append() as fd:
            size = os.fstat(fd).st_size
            # A torn last line (crash mid-write) is terminated, never merged into.
            torn = size > 0 and _read_block(fd, size - 1, 1) != b"\n"
            entry = AuditEntry(
                timestamp=datetime.now(tz=timezone.utc),
                event_type=event_type,
                actor=actor,
                resource_id=resource_id,
                details=details or {},
                prev_hash=_last_entry_hash(fd, size),
                entry_hash="",
            )
            entry.entry_hash = _compute_entry_hash(entry)
            raw_line = _canonical_json(_entry_to_dict(entry))
            _write_all(fd, (b"\n" if torn else b"") + raw_line.encode("utf-8") + b"\n")
            os.fsync(fd)

        self._last_hash = entry.entry_hash
        return entry

    def verify(self) -> list[str]:
        """Re-verify the entire chain.

        Returns a list of error strings.  An empty list means the log is intact.
        """
        errors: list[str] = []
        if not self._path.exists():
            return errors

        prev_entry_hash: str | None = None
        with self._locked_for_read(), self._path.open("r", encoding="utf-8") as fh:
            for lineno, raw_line in enumerate(fh, start=1):
                raw_line = raw_line.rstrip("\n")
                if not raw_line:
                    continue

                try:
                    d = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    errors.append(f"line {lineno}: invalid JSON — {exc}")
                    continue

                stored_entry_hash: str = d.get("entry_hash", "")
                stored_prev_hash: str | None = d.get("prev_hash")

                # Use the recomputed (not stored) hash from the previous
                # iteration so that a tampered earlier entry cascades into a
                # chain-break error here, rather than silently passing.
                if stored_prev_hash != prev_entry_hash:
                    errors.append(
                        f"line {lineno}: prev_hash mismatch "
                        f"(stored={stored_prev_hash!r}, expected={prev_entry_hash!r})"
                    )

                d_no_hash = {k: v for k, v in d.items() if k != "entry_hash"}
                recomputed = _sha256(_canonical_json(d_no_hash))
                if stored_entry_hash != recomputed:
                    errors.append(
                        f"line {lineno}: entry_hash mismatch "
                        f"(stored={stored_entry_hash!r}, recomputed={recomputed!r})"
                    )

                # Advance with the recomputed hash so downstream entries cannot
                # inherit a tampered stored value and pass the chain check.
                prev_entry_hash = recomputed

        return errors

    def select(self, event_types: Collection[str]) -> list[dict[str, Any]]:
        """Entries of the given types whose own ``entry_hash`` recomputes, as dicts.

        For callers that must *rely* on an entry (e.g. a bypass needs its
        ``seal.bypass.create``): corrupt lines and entries whose hash does not
        recompute are skipped, never raised, so one damaged line cannot hide or
        forge the rest. This checks each entry's self-consistency, not the chain
        (that is :meth:`verify`). Streams the file under a shared lock.
        """
        wanted = frozenset(event_types)
        found: list[dict[str, Any]] = []
        if not self._path.exists():
            return found
        with self._locked_for_read(), self._path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    d = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(d, dict) or d.get("event_type") not in wanted:
                    continue
                body = {k: v for k, v in d.items() if k != "entry_hash"}
                if d.get("entry_hash") == _sha256(_canonical_json(body)):
                    found.append(d)
        return found

    def query(self, resource_id: str | None = None) -> list[AuditEntry]:
        """Return all entries, optionally filtered by *resource_id*."""
        entries: list[AuditEntry] = []
        if not self._path.exists():
            return entries

        with self._locked_for_read(), self._path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.rstrip("\n")
                if not raw_line:
                    continue
                entry = AuditEntry.model_validate_json(raw_line)
                if resource_id is None or entry.resource_id == resource_id:
                    entries.append(entry)

        return entries
