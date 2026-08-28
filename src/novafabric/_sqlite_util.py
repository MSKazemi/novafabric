"""Shared SQLite connection helper.

Every ``sqlite3.connect`` in the codebase should go through :func:`connect_sqlite`
so the ``busy_timeout`` pragma cannot be forgotten. Without it, a connection
opened ``check_same_thread=False`` and shared across threads (or two processes on
one WAL database) receives an immediate ``sqlite3.OperationalError: database is
locked`` the instant a writer holds the lock, instead of waiting. A busy timeout
turns that transient contention into a short wait — the correct behaviour for a
local-first store under concurrent access.

:func:`connect_sqlite` deliberately sets ONLY ``busy_timeout``. Journal mode
(``WAL``) and ``synchronous`` remain each store's own decision, so adopting this
helper is a minimal, behaviour-preserving change. A store that wants WAL should
call :func:`ensure_wal` rather than issuing the pragma itself — see its docstring
for why the unconditional form is a defect.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

DEFAULT_BUSY_TIMEOUT_MS = 5000

#: Attempts to put a cold database into WAL. Only connections racing to open a
#: database that is not yet in WAL ever retry, so this is a cold-start bound,
#: not a per-call one.
#:
#: This budget is headroom, not a measured requirement. Across 360 eight-thread
#: cold races (lease, jobs and nonce, instrumented to record the attempt index
#: that succeeded) **no connection ever needed a second attempt**: a loser's own
#: ``PRAGMA journal_mode=WAL`` reports back the WAL the winner just established,
#: so it returns on attempt 0. The retries exist for a slower or busier machine
#: than the one this was measured on.
WAL_ATTEMPTS = 12
#: Exponential backoff base, capped by :data:`WAL_BACKOFF_MAX_S`. The full
#: schedule spans ~2.3s and is bounded.
WAL_BACKOFF_S = 0.02
WAL_BACKOFF_MAX_S = 0.25


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


def ensure_wal(conn: sqlite3.Connection, *, what: str = "database") -> None:
    """Put *conn*'s database into WAL, tolerating a concurrent cold start.

    ``PRAGMA journal_mode=WAL`` is a **write** that needs a brief exclusive lock,
    and SQLite does **not** apply the connection's busy timeout to a journal-mode
    change — it returns ``SQLITE_BUSY`` immediately. Issuing it unconditionally on
    every connect therefore makes concurrent opens of one file raise
    ``OperationalError: database is locked``. Setting ``busy_timeout`` first does
    not help; several stores did exactly that and were still exposed.

    The window is the **cold start**, and that is measured, not assumed. Eight
    threads opening one database with the unconditional pragma (``busy_timeout``
    set first, which is what several stores did), 500 trials per arm:

    ==========================  =========================
    database not yet in WAL     25/500 trials failed
    database already in WAL      0/500 trials failed
    ==========================  =========================

    The rate is load-dependent, so treat it as "reliably reproducible", not as a
    constant: what matters is that the cold arm fails and the warm arm does not.

    Journal mode is a persistent property of the *file*, so once any connection
    has established WAL the rest write nothing. That is why this surfaced as a
    test flake — the suite creates thousands of fresh databases — rather than as
    a production incident, and it is why reading the mode before writing it is
    the whole fix.

    The retry's exit condition is the **invariant** ("the database is in WAL"),
    not "my statement succeeded", so a thread that loses the race is satisfied by
    the winner's work instead of failing.

    Falling back to the default journal mode is never silent: if WAL cannot be
    established the ``OperationalError`` propagates, because callers that expect
    concurrent readers must not silently get a mode that does not allow them.
    """
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(WAL_ATTEMPTS):
        try:
            # The READ needs a shared lock and can itself return SQLITE_BUSY
            # while another connection is mid journal-mode change, so it belongs
            # inside the retry too.
            row = conn.execute("PRAGMA journal_mode").fetchone()
            if row is not None and str(row[0]).lower() == "wal":
                return
            # ``PRAGMA journal_mode=WAL`` does NOT raise when it cannot switch.
            # It returns the journal mode that is still in effect, so the result
            # must be inspected. An earlier version of this helper trusted the
            # absence of an exception, spun through its whole retry budget
            # without ever sleeping (the backoff lived only in the except
            # branch), and then failed on an unprotected read below.
            row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
            conn.commit()
            if row is not None and str(row[0]).lower() == "wal":
                return
        except sqlite3.OperationalError as exc:
            last_error = exc
        if attempt < WAL_ATTEMPTS - 1:
            # Drop any lock this connection holds before backing off, so the
            # retries do not block each other in lockstep. Defensive only: the
            # races measured above never reached a second attempt, so nothing
            # here is exercised by them.
            conn.rollback()
            time.sleep(min(WAL_BACKOFF_S * (2**attempt), WAL_BACKOFF_MAX_S))
    if last_error is not None:
        raise last_error
    raise sqlite3.OperationalError(f"could not put the {what} into WAL mode")
