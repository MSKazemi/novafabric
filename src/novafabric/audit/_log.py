from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._models import AuditEntry, AuditEventType

log = logging.getLogger(__name__)


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
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._last_hash: str | None = None
        # Replay existing file to recover the last entry_hash.
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.rstrip("\n")
                    if line:
                        try:
                            d = json.loads(line)
                            self._last_hash = d.get("entry_hash") or None
                        except json.JSONDecodeError:
                            log.warning(
                                "audit log: skipping corrupt line during init: %r",
                                line,
                            )

    def append(
        self,
        event_type: AuditEventType,
        actor: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
    ) -> AuditEntry:
        """Append one entry to the log and return the finalised :class:`AuditEntry`."""
        entry = AuditEntry(
            timestamp=datetime.now(tz=timezone.utc),
            event_type=event_type,
            actor=actor,
            resource_id=resource_id,
            details=details or {},
            prev_hash=self._last_hash,
            entry_hash="",
        )
        entry.entry_hash = _compute_entry_hash(entry)

        d = _entry_to_dict(entry)
        raw_line = _canonical_json(d)

        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(raw_line + "\n")

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
        with self._path.open("r", encoding="utf-8") as fh:
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

    def query(self, resource_id: str | None = None) -> list[AuditEntry]:
        """Return all entries, optionally filtered by *resource_id*."""
        entries: list[AuditEntry] = []
        if not self._path.exists():
            return entries

        with self._path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.rstrip("\n")
                if not raw_line:
                    continue
                entry = AuditEntry.model_validate_json(raw_line)
                if resource_id is None or entry.resource_id == resource_id:
                    entries.append(entry)

        return entries
