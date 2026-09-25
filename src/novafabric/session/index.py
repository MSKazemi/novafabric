"""Local SQLite session index — a rebuildable cache (ADR-0122 P3, experimental).

``nova session list`` used to parse every ``session.json`` under the sessions
root on every call. This module adds a small SQLite file,
``<sessions-root>/.session-index.sqlite``, that caches each parsed manifest
so enumeration reads one table instead of N JSON files. It is **not** the
registry SQLite schema (that one is protected); it lives next to the session
manifests it describes and is owned by this module alone.

Design contract:

- **The manifests stay authoritative.** The index is a cache: deleting it
  loses nothing, and :func:`rebuild_index` regenerates it from the manifests.
- **Fail-safe reads.** A missing, corrupt, wrong-version, or stale index never
  fails a listing — :func:`list_sessions_fast` falls back to the directory
  scan (:func:`~novafabric.session.manifest.list_sessions`) and reports *why*
  in :attr:`SessionListing.index_status`, so the caller can offer a rebuild.
- **Freshness without parsing.** An index row records the manifest's
  ``(mtime_ns, size, inode)``. A listing stats each ``<id>/session.json``
  (no JSON parsing, no capsule-directory access) and serves from the index
  only when every stat matches; any drift is ``stale``.
- **Write-through, never create.** :func:`upsert_session` (called by
  :func:`~novafabric.session.manifest.save_session`) refreshes one row when an
  index already exists; it never creates one, so a partial index can never
  masquerade as complete. Only :func:`rebuild_index` creates the file.
- **Concurrency.** WAL journal + ``busy_timeout``; every write is one
  ``BEGIN IMMEDIATE`` transaction, so a concurrent reader sees either the old
  or the new rows, never a half-rebuilt table. Reads never write: they open
  the index with a read-only URI (``mode=ro``), so a listing never creates
  the index file, and an index that vanishes between the existence check
  and the open reads as ``missing`` (scan fallback), not ``corrupt``.

Stdlib only (``sqlite3``) — Tier A per ADR-0024.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from novafabric.session.manifest import (
    SESSION_MANIFEST_FILENAME,
    SessionError,
    SessionIntegrityError,
    SessionManifest,
    list_sessions,
    load_manifest_file,
    session_dir_entries,
    sessions_root,
)

logger = logging.getLogger(__name__)

#: Index file name, inside the sessions root. The leading dot keeps it (and its
#: ``-wal``/``-shm`` siblings) out of the way of the ``<session_id>/`` dirs.
SESSION_INDEX_FILENAME = ".session-index.sqlite"
#: Bumped on any incompatible table change; a mismatch reads as ``version_mismatch``
#: and the index is rebuilt from scratch rather than migrated (it is a cache).
SESSION_INDEX_VERSION = 1
#: Milliseconds a writer waits on a locked index before giving up.
BUSY_TIMEOUT_MS = 5000

IndexStatus = Literal["fresh", "missing", "stale", "corrupt", "version_mismatch"]
ListingSource = Literal["index", "scan"]

_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    dir_name       TEXT PRIMARY KEY,
    manifest_mtime INTEGER NOT NULL,
    manifest_size  INTEGER NOT NULL,
    manifest_inode INTEGER NOT NULL,
    readable       INTEGER NOT NULL,
    manifest_json  TEXT
)
"""


class SessionIndexError(SessionError):
    """The session index could not be written (rebuild/upsert failed)."""


class SessionListing(BaseModel):
    """Result of :func:`list_sessions_fast` — manifests plus where they came from."""

    manifests: list[SessionManifest]
    #: ``index`` when served from a fresh index; ``scan`` on any fallback.
    source: ListingSource
    index_status: IndexStatus
    #: Human-readable reason for a fallback (``None`` when fresh).
    detail: str | None = None


class RebuildReport(BaseModel):
    """What :func:`rebuild_index` wrote."""

    index_path: str
    indexed: int
    #: Manifests present on disk but unreadable (recorded, excluded from listings).
    unreadable: int


def index_path(root: Path | None = None) -> Path:
    """``<sessions-root>/.session-index.sqlite``."""
    return sessions_root(root) / SESSION_INDEX_FILENAME


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    """Open *path* with WAL + busy_timeout; always closes the connection."""
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    with closing(conn):
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        yield conn


@contextmanager
def _connect_ro(path: Path) -> Iterator[sqlite3.Connection]:
    """Open *path* read-only; never creates the database file.

    Raises:
        sqlite3.OperationalError: The file does not exist (or cannot be opened).
    """
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    with closing(conn):
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        yield conn


def _stat_key(path: Path) -> tuple[int, int, int] | None:
    """``(mtime_ns, size, inode)`` of *path*, or ``None`` if it is not a file."""
    try:
        st = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _disk_state(base: Path) -> dict[str, tuple[int, int, int]]:
    """Stat every ``<dir>/session.json`` under *base* (no parsing).

    Uses the same entry filter as the authoritative scan
    (:func:`~novafabric.session.manifest.session_dir_entries`), so a fresh
    index always describes exactly the set of sessions a scan would list.
    """
    state: dict[str, tuple[int, int, int]] = {}
    for name, path in session_dir_entries(base):
        key = _stat_key(path)
        if key is not None:
            state[name] = key
    return state


def _row_for(base: Path, dir_name: str) -> tuple[str, int, int, int, int, str | None] | None:
    """Build one index row from ``<base>/<dir_name>/session.json``.

    Stat is taken *before* the read, so a manifest rewritten mid-read leaves a
    row whose stat no longer matches disk — it reads back as ``stale`` rather
    than serving the torn content as fresh.
    """
    path = base / dir_name / SESSION_MANIFEST_FILENAME
    key = _stat_key(path)
    if key is None:
        return None
    try:
        manifest = load_manifest_file(path)
    except SessionIntegrityError as exc:
        logger.warning("Session index: recording unreadable manifest: %s", exc)
        return (dir_name, *key, 0, None)
    return (dir_name, *key, 1, json.dumps(manifest.to_json_dict(), sort_keys=True))


def rebuild_index(root: Path | None = None) -> RebuildReport:
    """(Re)create the index from every manifest under the sessions root.

    A corrupt or wrong-version file is discarded and recreated. The row swap
    runs in a single ``BEGIN IMMEDIATE`` transaction, so concurrent readers
    see the old index or the new one, never a partial table.

    Raises:
        SessionIndexError: The index file cannot be created or written.
    """
    base = sessions_root(root)
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SessionIndexError(f"cannot create sessions root {base}: {exc}") from exc
    path = index_path(root)
    if path.exists() and _probe(path)[0] in ("corrupt", "version_mismatch"):
        _discard(path)
    rows = [
        row
        for row in (_row_for(base, name) for name in sorted(_disk_state(base)))
        if row is not None
    ]
    try:
        with _connect(path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(_DDL)
                conn.execute("DELETE FROM sessions")
                conn.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)", rows)
                conn.execute(f"PRAGMA user_version={SESSION_INDEX_VERSION}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
    except sqlite3.Error as exc:
        raise SessionIndexError(f"cannot rebuild session index {path}: {exc}") from exc
    return RebuildReport(
        index_path=str(path),
        indexed=sum(1 for r in rows if r[4] == 1),
        unreadable=sum(1 for r in rows if r[4] == 0),
    )


def _discard(path: Path) -> None:
    """Remove an unusable index and its WAL siblings (it is only a cache)."""
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            continue


def _probe(path: Path) -> tuple[IndexStatus, str | None]:
    """Classify an existing index file without modifying it."""
    try:
        with _connect_ro(path) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version != SESSION_INDEX_VERSION:
                return (
                    "version_mismatch",
                    f"index version {version}, expected {SESSION_INDEX_VERSION}",
                )
            conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
    except sqlite3.Error as exc:
        return ("corrupt", f"unreadable index {path}: {exc}")
    return ("fresh", None)


def upsert_session(dir_name: str, root: Path | None = None) -> bool:
    """Refresh one session's row, **only** if an index already exists.

    Best-effort and non-blocking: a failure is logged and swallowed (the
    listing will detect the stale row and fall back to a scan). Never creates
    an index — a one-row index would look complete while missing sessions.

    Returns:
        True when a row was written.
    """
    path = index_path(root)
    if not path.is_file():
        return False
    row = _row_for(sessions_root(root), dir_name)
    if row is None:
        return False
    try:
        with _connect(path) as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] != SESSION_INDEX_VERSION:
                return False
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?, ?)", row)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
    except sqlite3.Error as exc:
        logger.warning("Session index not updated for %s: %s", dir_name, exc)
        return False
    return True


def _read_index(
    path: Path,
) -> tuple[dict[str, tuple[int, int, int]], list[tuple[str, str]]]:
    """All rows: ``{dir: stat}`` plus ``[(dir, manifest_json)]`` for readable ones."""
    with _connect_ro(path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SESSION_INDEX_VERSION:
            raise _VersionMismatch(version)
        rows = conn.execute(
            "SELECT dir_name, manifest_mtime, manifest_size, manifest_inode, "
            "readable, manifest_json FROM sessions"
        ).fetchall()
    stats = {r[0]: (int(r[1]), int(r[2]), int(r[3])) for r in rows}
    readable = [(r[0], r[5]) for r in rows if r[4] == 1 and r[5] is not None]
    return stats, readable


class _VersionMismatch(Exception):
    """Internal signal: the index file has a different ``user_version``."""

    def __init__(self, version: int) -> None:
        super().__init__(version)
        self.version = version


def _scan(root: Path | None, status: IndexStatus, detail: str) -> SessionListing:
    return SessionListing(
        manifests=list_sessions(root=root),
        source="scan",
        index_status=status,
        detail=detail,
    )


def list_sessions_fast(root: Path | None = None) -> SessionListing:
    """List sessions (newest first) from the index, falling back to a scan.

    Served from the index only when it exists, opens, has the current version,
    and every row's manifest stat matches disk exactly (same session set, same
    ``(mtime_ns, size, inode)``). Otherwise the result comes from the
    directory scan and ``index_status`` says why. Never raises for index
    problems and never writes (safe on a read-only sessions root).
    """
    path = index_path(root)
    if not path.is_file():
        return _scan(root, "missing", f"no session index at {path}")
    try:
        indexed_stats, readable = _read_index(path)
    except _VersionMismatch as exc:
        return _scan(
            root,
            "version_mismatch",
            f"index version {exc.version}, expected {SESSION_INDEX_VERSION}",
        )
    except sqlite3.Error as exc:
        if not path.exists():  # removed between the check and the open
            return _scan(root, "missing", f"session index {path} vanished before it was read")
        return _scan(root, "corrupt", f"unreadable index {path}: {exc}")

    disk = _disk_state(sessions_root(root))
    if disk != indexed_stats:
        changed = sorted(set(disk) ^ set(indexed_stats)) or sorted(
            name for name in disk if disk[name] != indexed_stats.get(name)
        )
        preview = ", ".join(changed[:3]) + (" …" if len(changed) > 3 else "")
        return _scan(
            root,
            "stale",
            f"index out of date for {len(changed)} session(s): {preview}",
        )

    manifests: list[SessionManifest] = []
    for _dir_name, raw in sorted(readable, key=lambda item: item[0], reverse=True):
        try:
            manifests.append(SessionManifest.model_validate(json.loads(raw)))
        except ValueError as exc:
            return _scan(root, "corrupt", f"undecodable index row: {exc}")
    return SessionListing(manifests=manifests, source="index", index_status="fresh")
