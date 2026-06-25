"""Tier 2 entity resolution — SQLite-backed alias table with in-memory TTL cache.

Architecture (three-tier):
  Tier 1 (EntityNormaliser)    — pure-Python synchronous normalisation, LRU cached
  Tier 2 (AliasTableResolver)  — persistent alias table in SQLite, TTL in-memory cache
  Tier 3 (HumanReviewQueueWriter) — unresolved entity queue for manual review

The resolver is fully synchronous and thread-safe (threading.RLock).
No Postgres / asyncpg dependency — SQLite is stdlib.

Heuristic version-stripping
----------------------------
If ``resolve()`` finds no direct entry for *alias*, it tries stripping common
version suffixes before giving up:

  * trailing ``-YYYY-MM-DD``   → ``gpt-4o-2024-05-13`` → ``gpt-4o``
  * trailing ``-v<digits>``    → ``mymodel-v3``         → ``mymodel``
  * leading ``<vendor>/``      → ``openai/gpt-4o``      → ``gpt-4o``

Only the first match is tried; if the stripped form resolves, the result is
returned (confidence unchanged from the stored alias entry).
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ── heuristic patterns applied in order ────────────────────────────────────
_VERSION_SUFFIX_DATE = re.compile(r"^(.+?)-\d{4}-\d{2}-\d{2}$")
_VERSION_SUFFIX_V = re.compile(r"^(.+?)-v\d+$")
_VERSION_SUFFIX_SLASH_V = re.compile(r"^(.+?)/v\d+$")
_VENDOR_PREFIX = re.compile(r"^[^/]+/(.+)$")

_HEURISTIC_PATTERNS: list[re.Pattern[str]] = [
    _VERSION_SUFFIX_DATE,
    _VERSION_SUFFIX_V,
    _VERSION_SUFFIX_SLASH_V,
    _VENDOR_PREFIX,
]

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS alias_table (
    alias        TEXT NOT NULL,
    canonical    TEXT NOT NULL,
    entity_type  TEXT NOT NULL,
    confidence   REAL NOT NULL DEFAULT 1.0,
    source       TEXT NOT NULL DEFAULT 'manual',
    created_at   TEXT NOT NULL,
    PRIMARY KEY (alias, entity_type)
)
"""

_INSERT_SQL = """
INSERT INTO alias_table (alias, canonical, entity_type, confidence, source, created_at)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(alias, entity_type)
DO UPDATE SET
    canonical   = excluded.canonical,
    confidence  = excluded.confidence,
    source      = excluded.source
"""


class AliasEntry(BaseModel):
    alias: str
    canonical: str
    entity_type: str
    confidence: float = 1.0
    source: str = "manual"
    created_at: datetime


class AliasTableResolver:
    """Thread-safe SQLite-backed alias table with in-memory TTL cache.

    Usage::

        resolver = AliasTableResolver(db_path=Path(".nova/alias.db"))
        canonical = resolver.resolve("gpt-4o-2024-05-13", "model")
        # → "gpt-4o"  (if registered)  or  None

    All public methods acquire ``_lock`` (threading.RLock) for full thread safety.
    """

    def __init__(
        self,
        db_path: Path,
        cache_ttl_seconds: int = 300,
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_ttl = cache_ttl_seconds
        # cache: (alias, entity_type) → (canonical, expires_at)
        self._cache: dict[tuple[str, str], tuple[str, float]] = {}
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    # ------------------------------------------------------------------
    # Internal setup
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute(_CREATE_TABLE_SQL)
            conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        """Return the shared connection (create on first call).

        Called under self._lock.
        """
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    # ------------------------------------------------------------------
    # Cache helpers (caller must hold _lock)
    # ------------------------------------------------------------------

    def _cache_get(self, alias: str, entity_type: str) -> str | None:
        key = (alias, entity_type)
        entry = self._cache.get(key)
        if entry is None:
            return None
        canonical, expires_at = entry
        if time.monotonic() > expires_at:
            del self._cache[key]
            return None
        return canonical

    def _cache_set(self, alias: str, entity_type: str, canonical: str) -> None:
        key = (alias, entity_type)
        self._cache[key] = (canonical, time.monotonic() + self._cache_ttl)

    # ------------------------------------------------------------------
    # Heuristic version-stripping
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_version(alias: str) -> str | None:
        """Return the alias with version suffix / vendor prefix stripped, or None."""
        for pattern in _HEURISTIC_PATTERNS:
            m = pattern.match(alias)
            if m:
                return m.group(1)
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(self, alias: str, entity_type: str) -> str | None:
        """Look up *alias* in the alias table.

        Resolution order:
        1. In-memory TTL cache (exact key)
        2. SQLite lookup (exact key)
        3. Heuristic version-strip → re-check cache → re-check SQLite
        4. Return None (not found)

        Results from steps 2–3 are written into the cache.
        """
        with self._lock:
            # 1. cache (exact)
            cached = self._cache_get(alias, entity_type)
            if cached is not None:
                return cached

            conn = self._get_conn()

            # 2. SQLite (exact)
            row = conn.execute(
                "SELECT canonical FROM alias_table WHERE alias = ? AND entity_type = ?",
                (alias, entity_type),
            ).fetchone()
            if row is not None:
                canonical: str = row["canonical"]
                self._cache_set(alias, entity_type, canonical)
                return canonical

            # 3. heuristic version-strip
            stripped = self._strip_version(alias)
            if stripped is not None:
                cached_stripped = self._cache_get(stripped, entity_type)
                if cached_stripped is not None:
                    self._cache_set(alias, entity_type, cached_stripped)
                    return cached_stripped
                row2 = conn.execute(
                    "SELECT canonical FROM alias_table WHERE alias = ? AND entity_type = ?",
                    (stripped, entity_type),
                ).fetchone()
                if row2 is not None:
                    canonical = row2["canonical"]
                    self._cache_set(alias, entity_type, canonical)
                    return canonical

            return None

    def register(self, entry: AliasEntry) -> None:
        """Insert or update a single alias entry."""
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                _INSERT_SQL,
                (
                    entry.alias,
                    entry.canonical,
                    entry.entity_type,
                    entry.confidence,
                    entry.source,
                    entry.created_at.isoformat(),
                ),
            )
            conn.commit()
            # Invalidate cache for this key
            self._cache.pop((entry.alias, entry.entity_type), None)
            logger.debug(
                "AliasTableResolver: registered %r → %r (%s)",
                entry.alias,
                entry.canonical,
                entry.entity_type,
            )

    def bulk_register(self, entries: list[AliasEntry]) -> None:
        """Insert or update multiple alias entries in a single transaction."""
        if not entries:
            return
        with self._lock:
            conn = self._get_conn()
            conn.executemany(
                _INSERT_SQL,
                [
                    (
                        e.alias,
                        e.canonical,
                        e.entity_type,
                        e.confidence,
                        e.source,
                        e.created_at.isoformat(),
                    )
                    for e in entries
                ],
            )
            conn.commit()
            for e in entries:
                self._cache.pop((e.alias, e.entity_type), None)
            logger.debug(
                "AliasTableResolver: bulk-registered %d entries", len(entries)
            )

    def list_aliases(self, canonical: str) -> list[AliasEntry]:
        """Return all aliases that map to *canonical* (any entity_type)."""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT alias, canonical, entity_type, confidence, source, created_at "
                "FROM alias_table WHERE canonical = ? ORDER BY alias",
                (canonical,),
            ).fetchall()
            return [
                AliasEntry(
                    alias=r["alias"],
                    canonical=r["canonical"],
                    entity_type=r["entity_type"],
                    confidence=r["confidence"],
                    source=r["source"],
                    created_at=datetime.fromisoformat(r["created_at"]),
                )
                for r in rows
            ]

    def clear_cache(self) -> None:
        """Flush the in-memory TTL cache."""
        with self._lock:
            self._cache.clear()
            logger.debug("AliasTableResolver: in-memory cache cleared")

    def close(self) -> None:
        """Close the SQLite connection."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
