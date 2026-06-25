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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from novafabric._paths import dashboard_audit_path

AUDIT_ENV: Final[str] = "NOVAFABRIC_DASHBOARD_AUDIT_FILE"

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


def read_recent(limit: int = 200) -> list[dict[str, Any]]:
    """Return the most recent audit entries, newest first."""
    p = _path()
    if not p.exists():
        return []
    with _lock, p.open("r", encoding="utf-8") as f:
        lines = f.readlines()
    out: list[dict[str, Any]] = []
    for line in reversed(lines):
        s = line.strip()
        if not s:
            continue
        try:
            out.append(json.loads(s))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    return out
