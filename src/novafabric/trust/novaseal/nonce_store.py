"""RFC 3161 nonce replay protection for NovaSeal (ADR-0070).

Nonce replay protection prevents an attacker from replaying a previously
issued TimeStampRequest and associated TSR against a different payload.

Design:
  - Nonces are generated via ``secrets.randbits(64)`` — 64-bit random integers
    with ~2^64 collision resistance.  Statistical replay probability is
    negligible; the guard is defensive-in-depth.
  - Seen nonces are persisted in a SQLite database (same engine as the Merkle
    log; no new runtime dependency).
  - Old nonces are pruned via ``prune_old_nonces(max_age_days)``.
  - The nonce store is a no-op when *offline_mode* is True (HPC air-gap mode);
    callers pass ``offline_mode=True`` to skip all reads/writes.

Database schema:

    CREATE TABLE nonces (
        nonce  INTEGER PRIMARY KEY,
        seen_at REAL NOT NULL   -- Unix timestamp (float)
    );
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

from novafabric._sqlite_util import connect_sqlite

logger = logging.getLogger(__name__)

_DEFAULT_NONCES_DB_NAME = "tsa_nonces.db"


def _resolve_nonce_store_path(path: Optional[Path]) -> Path:
    """Return the resolved path to the nonce store database.

    Resolution order:
    1. Explicit *path* argument (not None).
    2. ``$NOVAFABRIC_HOME/tsa_nonces.db``.
    3. ``~/.novafabric/tsa_nonces.db``.
    """
    if path is not None:
        return path
    home_env = os.environ.get("NOVAFABRIC_HOME")
    if home_env:
        base = Path(home_env)
    else:
        base = Path.home() / ".novafabric"
    return base / _DEFAULT_NONCES_DB_NAME


class NonceStore:
    """SQLite-backed store for RFC 3161 request nonces (ADR-0070).

    Args:
        path:         Explicit path to the SQLite database.  When *None*, the
                      path is auto-derived from ``$NOVAFABRIC_HOME`` or
                      ``~/.novafabric/`` (see ``_resolve_nonce_store_path``).
        offline_mode: When *True*, all operations are no-ops.  Use for HPC
                      air-gapped environments where no disk I/O is desired at
                      timestamp request time.
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        offline_mode: bool = False,
    ) -> None:
        self._offline = offline_mode
        if self._offline:
            self._db_path: Optional[Path] = None
            return
        self._db_path = _resolve_nonce_store_path(path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema initialisation
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nonces (
                    nonce   INTEGER PRIMARY KEY,
                    seen_at REAL    NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_nonces_seen_at ON nonces (seen_at)"
            )

    def _connect(self) -> sqlite3.Connection:
        assert self._db_path is not None
        conn = connect_sqlite(str(self._db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_nonce(self, nonce: int) -> None:
        """Persist *nonce* as seen.

        Silently ignores duplicates (``INSERT OR IGNORE``); callers should
        call :py:meth:`is_replay` first if they want to detect replays.
        """
        if self._offline:
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO nonces (nonce, seen_at) VALUES (?, ?)",
                (nonce, time.time()),
            )

    def is_replay(self, nonce: int) -> bool:
        """Return *True* if *nonce* has been seen before.

        Args:
            nonce: 64-bit unsigned integer nonce from a TSR request.

        Returns:
            ``True``  — nonce already in the store (replay detected).
            ``False`` — nonce is fresh (not seen before) or offline mode.
        """
        if self._offline:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM nonces WHERE nonce = ? LIMIT 1", (nonce,)
            ).fetchone()
        return row is not None

    def prune_old_nonces(self, max_age_days: int = 30) -> int:
        """Delete nonces older than *max_age_days*.

        Args:
            max_age_days: Entries seen more than this many days ago are removed.

        Returns:
            Number of rows deleted.
        """
        if self._offline:
            return 0
        cutoff = time.time() - max_age_days * 86400.0
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM nonces WHERE seen_at < ?", (cutoff,)
            )
            deleted = cur.rowcount
        if deleted:
            logger.info(
                "nonce_store: pruned %d old nonces (max_age=%d days)", deleted, max_age_days
            )
        return deleted
