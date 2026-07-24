"""Shared SQLite connection helper.

Every ``sqlite3.connect`` in the codebase should go through :func:`connect_sqlite`
so the ``busy_timeout`` pragma cannot be forgotten. Without it, a connection
opened ``check_same_thread=False`` and shared across threads (or two processes on
one WAL database) receives an immediate ``sqlite3.OperationalError: database is
locked`` the instant a writer holds the lock, instead of waiting. A busy timeout
turns that transient contention into a short wait — the correct behaviour for a
local-first store under concurrent access.

The helper deliberately sets ONLY ``busy_timeout``. Journal mode
(``WAL``) and ``synchronous`` remain each store's own decision, so adopting this
helper is a minimal, behaviour-preserving change.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_BUSY_TIMEOUT_MS = 5000


def connect_sqlite(
    db_path: str | Path,
    *,
    check_same_thread: bool = True,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """Open a SQLite connection with ``busy_timeout`` set.

    Args mirror ``sqlite3.connect`` for the options we use; callers remain free
    to set journal mode / synchronous afterwards.
    """
    conn = sqlite3.connect(str(db_path), check_same_thread=check_same_thread)
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    return conn
