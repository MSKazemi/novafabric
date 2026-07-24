"""Append-only audit log for `nova serve` mutations (Layer B).

Per ADR-0027 §1 Layer B: every mutation is recorded in
`~/.novafabric/dashboard-audit.jsonl`, one record per line, append-only.
The user can `cat` it, grep it, or pipe it into their own tooling. There
is no rotation / no truncation; entries are tiny (few hundred bytes each).
"""

from __future__ import annotations

import json
import os
import stat
import threading
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from novafabric._paths import dashboard_audit_path

AUDIT_ENV: Final[str] = "NOVAFABRIC_DASHBOARD_AUDIT_FILE"

#: Block size for reverse (tail-first) reads. 64 KiB covers hundreds of
#: typical few-hundred-byte audit records per seek.
TAIL_BLOCK_SIZE: Final[int] = 64 * 1024

_lock = threading.Lock()


def _path() -> Path:
    return dashboard_audit_path()


def append(
    *,
    action: str,
    args: dict[str, Any],
    cli_equivalent: str,
    actor_token_fp: str,
    result: str = "ok",
    error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one audit record. Returns the record (caller may include it in API response).

    `actor_token_fp` is a short fingerprint of the session token (first 8 chars), not
    the full token. The token is the only identity we have on a localhost server,
    so logging a short fingerprint helps correlate sessions without leaking the token.
    """
    record: dict[str, Any] = {
        "audit_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "args": args,
        "cli_equivalent": cli_equivalent,
        "actor_token_fp": actor_token_fp,
        "result": result,
    }
    if error:
        record["error"] = error
    if extra:
        record["extra"] = extra

    p = _path()
    with _lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
        # Ensure read/write only by owner
        try:
            os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    return record


def tail_lines(
    path: Path,
    *,
    before_offset: int | None = None,
    block_size: int = TAIL_BLOCK_SIZE,
) -> Iterator[tuple[int, bytes]]:
    """Yield ``(byte_offset, line)`` newest-first from an append-only JSONL file.

    Reads backwards from EOF (or from ``before_offset``) in ``block_size``
    chunks, so IO is proportional to how far the caller iterates — never the
    whole file. Because the file is append-only, a line's byte offset is a
    stable resume cursor: pass it back as ``before_offset`` to continue with
    strictly older lines. Empty lines are skipped; a missing file yields
    nothing.
    """
    if not path.exists():
        return
    with path.open("rb") as f:
        size = f.seek(0, os.SEEK_END)
        pos = size if before_offset is None else max(0, min(before_offset, size))
        buf = b""
        while pos > 0:
            read_size = min(block_size, pos)
            pos -= read_size
            f.seek(pos)
            buf = f.read(read_size) + buf
            parts = buf.split(b"\n")
            if pos > 0:
                # First part may be the tail of a line straddling the block
                # boundary — keep it in the buffer for the next iteration.
                buf = parts[0]
                complete = parts[1:]
                base = pos + len(parts[0]) + 1
            else:
                buf = b""
                complete = parts
                base = 0
            offsets: list[int] = []
            off = base
            for line in complete:
                offsets.append(off)
                off += len(line) + 1
            for line_off, line in zip(reversed(offsets), reversed(complete)):
                if line.strip():
                    yield line_off, line


def read_recent_tail(
    limit: int = 200,
    before_offset: int | None = None,
    action: str | None = None,
) -> tuple[list[dict[str, Any]], int | None]:
    """Return up to *limit* recent audit entries, newest first, with a cursor.

    ``before_offset`` (a byte offset from a previous call's cursor) restricts
    the read to entries strictly before that file position; ``action`` keeps
    only entries whose ``action`` field matches exactly. Malformed lines are
    skipped. Returns ``(entries, next_before_offset)`` where the cursor is
    ``None`` once the file is exhausted.
    """
    p = _path()
    entries: list[dict[str, Any]] = []
    next_cursor: int | None = None
    with _lock:
        for off, raw in tail_lines(p, before_offset=before_offset):
            try:
                rec = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(rec, dict):
                continue
            if action is not None and rec.get("action") != action:
                continue
            entries.append(rec)
            if len(entries) >= limit:
                if off > 0:
                    next_cursor = off
                break
    return entries, next_cursor


def read_recent(limit: int = 200) -> list[dict[str, Any]]:
    """Return the most recent audit entries, newest first."""
    return read_recent_tail(limit)[0]
