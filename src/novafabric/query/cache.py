"""Persistent row cache for the `nova query` index (ADR-0225).

`run_query` used to re-scan and re-parse every capsule on every invocation.
ADR-0225 measured where that time goes: **discovery is 7.7% of the scan and
parsing is 92%**, so a cache that skips re-parsing unchanged capsules removes
roughly 13× of the scan work while still visiting every directory.

The design constraints, all from ADR-0225:

* **D2 — the cache lives outside the capsule directory.** Capsules are signed
  evidence and stay read-only; the cache goes under :func:`nova_home`.
* **D3 — degrade to correct, never to fast.** Any problem — unreadable file,
  schema mismatch, a row whose capsule is gone — falls back to a full scan.
  Wrong-but-fast is the one outcome an evidence tool may not produce.
* **D4 — single writer, many readers.** SQLite in WAL mode. A process that
  cannot take the write lock serves its query from a full scan rather than
  blocking: a query is a read and must never fail because another query was
  refreshing a cache.

## What counts as "unchanged" — a correction to ADR-0225 D1

D1 specified re-parsing a capsule "only if its **directory** mtime changed".
That is not sufficient, and the gap is reachable rather than theoretical:

    append to an existing scores.jsonl  ->  directory mtime does NOT change
    create a new file in the directory  ->  directory mtime changes

``eval.scores.append_score`` opens the file in ``"a"`` mode. So the *first*
score on a capsule creates ``scores.jsonl`` and moves the directory mtime, and
**every score after that is invisible** to a directory-mtime check —
``nova query 'avg(score[x])'`` would serve a stale answer indefinitely.

The ADR reasoned correctly about ``capsule.json`` being rewritten in place by
``ParentCapsuleTracker`` (caught, because that uses ``os.rename()``), and
generalised from that one mutation to the directory. ``scores.jsonl`` mutates
differently.

So the signature covers the **files the indexer actually reads** as well as the
directory, and pairs each mtime with a size. The size makes an append detectable
even inside the filesystem's mtime granularity, which closes the hole ADR-0225
lists under its own Consequences ▸ Negative.

The extra cost is four `stat` calls per capsule instead of one — still nothing
against the parse it avoids, which is the 92%.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import fields as dataclass_fields
from pathlib import Path

from novafabric._paths import nova_home
from novafabric.query.indexer import (
    INDEXED_FILENAMES,
    CallRow,
    IndexRows,
    ScoreRow,
    scan_capsule,
)

logger = logging.getLogger(__name__)

#: Bumped whenever the cache's own table layout changes. A mismatch discards
#: the cache wholesale rather than mixing generations (ADR-0225 D3).
CACHE_SCHEMA_VERSION = 1

#: Bumped whenever the *indexer* changes what it extracts. Stored alongside the
#: schema version because a NovaFabric upgrade can change the meaning of a row
#: without changing the table it lives in.
INDEXER_SCHEMA_VERSION = 1

#: Files whose content the indexer reads. Any change to one of these changes the
#: rows a capsule contributes, so each is part of the signature.
#:
#: Derived from the indexer rather than restated here: two lists of the same fact
#: agree until one of them is edited, and the divergence would be silent — a file
#: read but unsigned-for makes the cache serve stale rows, which is the one
#: outcome ADR-0225 D3 forbids. ``tests/query/test_indexer_signature_coupling.py``
#: fails if the indexer ever reads outside this set.
_INDEXED_FILES = INDEXED_FILENAMES

_CALL_FIELDS = tuple(f.name for f in dataclass_fields(CallRow))
_SCORE_FIELDS = tuple(f.name for f in dataclass_fields(ScoreRow))

# One row per capsule, holding all of its rows in a single payload. Storing one
# payload per *index row* was measured at 30 ms of JSON decoding for 2,000
# capsules — the cache would have been trading capsule parsing for payload
# parsing. One decode per capsule instead of one per row removes most of that.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capsules (
    capsule_key TEXT PRIMARY KEY,
    signature   TEXT NOT NULL,
    payload     TEXT NOT NULL
);
"""


def cache_db_path() -> Path:
    """Location of the query row cache: ``$NOVAFABRIC_HOME/query-index.db``."""
    return nova_home() / "query-index.db"


def capsule_signature(capsule_dir: Path) -> str:
    """Return a change signature for one capsule directory.

    Covers the directory (so a created or deleted file is caught) and every file
    the indexer reads (so an in-place append is caught — see the module
    docstring). Each contributes ``mtime_ns`` and ``size``; a missing file
    contributes a stable placeholder, so its later appearance is a change.
    """
    parts: list[str] = []
    try:
        st = capsule_dir.stat()
        parts.append(f"d:{st.st_mtime_ns}")
    except OSError:
        parts.append("d:-")
    for name in _INDEXED_FILES:
        try:
            st = (capsule_dir / name).stat()
            parts.append(f"{name}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append(f"{name}:-")
    return "|".join(parts)


def _encode(calls: list[CallRow], scores: list[ScoreRow]) -> str:
    """Serialize one capsule's index rows as a single compact payload.

    Rows are stored positionally rather than as objects: the field names are
    already pinned by ``_CALL_FIELDS`` / ``_SCORE_FIELDS``, and repeating them
    per row would dominate the payload.
    """
    return json.dumps(
        {
            "calls": [[getattr(r, f) for f in _CALL_FIELDS] for r in calls],
            "scores": [[getattr(r, f) for f in _SCORE_FIELDS] for r in scores],
        },
        separators=(",", ":"),
    )


def _decode(payload: str) -> tuple[list[CallRow], list[ScoreRow]]:
    """Rebuild one capsule's index rows. Raises on anything malformed.

    The caller treats a raised error as "rescan this capsule" — a damaged
    payload is a cache problem, never a query failure (D3).
    """
    data = json.loads(payload)
    return (
        [CallRow(**dict(zip(_CALL_FIELDS, values))) for values in data["calls"]],
        [ScoreRow(**dict(zip(_SCORE_FIELDS, values))) for values in data["scores"]],
    )


def _open(path: Path, *, write: bool) -> sqlite3.Connection | None:
    """Open the cache, returning ``None`` when it cannot be used.

    Never raises for a cache problem: an unusable cache is a performance
    outcome, not a correctness one (D3). ``write=True`` additionally takes the
    write lock immediately; failing to get it returns ``None`` so the caller
    serves from a full scan instead of waiting (D4).

    Deliberately not a context manager. Wrapping the caller's body would mean
    catching *its* exceptions too, and a ``@contextmanager`` cannot both handle
    a thrown-in error and yield a fallback value.
    """
    conn: sqlite3.Connection | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None puts transaction control here rather than in the
        # driver's implicit handling, which BEGIN IMMEDIATE below depends on.
        conn = sqlite3.connect(
            path, timeout=0.0 if write else 5.0, isolation_level=None
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        if write:
            # Take the write lock now rather than on first write, so contention
            # surfaces here — where it can be declined instead of waited on.
            conn.execute("BEGIN IMMEDIATE")
        if not _prepare_versions(conn, write=write):
            _close(conn)
            return None
        return conn
    except (sqlite3.Error, OSError) as exc:
        logger.debug("query cache unavailable (%s): serving from a full scan", exc)
        _close(conn)
        return None


def _close(conn: sqlite3.Connection | None) -> None:
    """Close a cache connection, discarding any error — closing cannot fail a query."""
    if conn is None:
        return
    try:
        conn.close()  # an open transaction is rolled back by close()
    except sqlite3.Error:
        pass


def _prepare_versions(conn: sqlite3.Connection, *, write: bool) -> bool:
    """Check the cache's generation, and on the write path adopt it.

    A cache written by a different schema or a different indexer is discarded
    wholesale (D3): mixing generations of extracted rows would produce answers
    that belong to neither generation. Readers simply decline a foreign cache —
    they have no business rewriting one — and fall back to a full scan.
    """
    expected = {
        "cache_schema_version": str(CACHE_SCHEMA_VERSION),
        "indexer_schema_version": str(INDEXER_SCHEMA_VERSION),
    }
    stored = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    if stored == expected:
        return True
    if not write:
        return False
    for table in ("capsules", "meta"):
        conn.execute(f"DELETE FROM {table}")  # noqa: S608 — fixed literals
    conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?)", sorted(expected.items())
    )
    return True


def _load_cached(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """Read the whole cache as ``{capsule_key: (signature, payload)}``."""
    return {
        key: (signature, payload)
        for key, signature, payload in conn.execute(
            "SELECT capsule_key, signature, payload FROM capsules"
        )
    }


def scan_capsule_dir_cached(
    capsule_dir: str | Path,
    *,
    use_cache: bool = True,
    rebuild: bool = False,
    cache_path: Path | None = None,
) -> IndexRows:
    """Scan a capsule directory, reusing cached rows for unchanged capsules.

    Equivalent to :func:`~novafabric.query.indexer.scan_capsule_dir` in every
    observable way — the cache changes how long the answer takes, never what it
    is. ``use_cache=False`` takes the plain scan; ``rebuild=True`` ignores the
    stored rows and rewrites them.
    """
    from novafabric.query.errors import QueryIndexError
    from novafabric.query.indexer import scan_capsule_dir

    base = Path(capsule_dir)
    if not use_cache:
        return scan_capsule_dir(base)
    if not base.is_dir():
        raise QueryIndexError(f"capsule directory not found: {base}")

    path = cache_path if cache_path is not None else cache_db_path()

    # Discovery pass — the part a persistent index does not remove. The base is
    # resolved once and child keys are built by name: resolving each child was
    # measured at 16.8 ms per 2,000 capsules, against 0.1 ms for the join.
    resolved_base = str(base.resolve())
    discovered: list[tuple[str, Path, str]] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        discovered.append(
            (f"{resolved_base}/{child.name}", child, capsule_signature(child))
        )

    cached: dict[str, tuple[str, str]] = {}
    if not rebuild:
        conn = _open(path, write=False)
        if conn is not None:
            try:
                cached = _load_cached(conn)
            except sqlite3.Error as exc:
                logger.debug("query cache unreadable (%s): full scan", exc)
                cached = {}
            finally:
                _close(conn)

    calls: list[CallRow] = []
    score_rows: list[ScoreRow] = []
    capsule_count = 0
    refreshed: dict[str, tuple[str, list[CallRow], list[ScoreRow]]] = {}
    live_keys: list[str] = []

    for key, child, signature in discovered:
        live_keys.append(key)
        entry = cached.get(key)
        if not rebuild and entry is not None and entry[0] == signature:
            try:
                capsule_calls, capsule_scores = _decode(entry[1])
            except (KeyError, TypeError, ValueError) as exc:
                # A damaged payload is a cache problem, not a query failure (D3).
                logger.debug("query cache row unusable for %s (%s): rescanning", key, exc)
            else:
                capsule_count += 1
                calls.extend(capsule_calls)
                score_rows.extend(capsule_scores)
                continue

        scanned = scan_capsule(child)
        if scanned is None:
            continue  # not a capsule
        capsule_count += 1
        capsule_calls, capsule_scores = scanned
        calls.extend(capsule_calls)
        score_rows.extend(capsule_scores)
        refreshed[key] = (signature, capsule_calls, capsule_scores)

    stale = set(cached) - set(live_keys)
    if refreshed or stale or rebuild:
        _write_back(path, refreshed, stale, rebuild=rebuild, live_keys=live_keys)

    return IndexRows(calls=calls, scores=score_rows, capsule_count=capsule_count)


def _write_back(
    path: Path,
    refreshed: dict[str, tuple[str, list[CallRow], list[ScoreRow]]],
    stale: set[str],
    *,
    rebuild: bool,
    live_keys: list[str],
) -> None:
    """Persist refreshed rows and drop rows whose capsule is gone.

    Pruning is not housekeeping: ADR-0225 D3 forbids serving a row whose source
    capsule no longer exists, so removal happens in the same transaction as the
    refresh (OQ-2). The whole thing is best-effort — a cache that cannot be
    written has cost the query nothing but a rebuild next time (D4).
    """
    conn = _open(path, write=True)
    if conn is None:
        return
    try:
        try:
            if rebuild:
                keep = set(live_keys)
                stored = [
                    row[0] for row in conn.execute("SELECT capsule_key FROM capsules")
                ]
                stale.update(key for key in stored if key not in keep)
            conn.executemany(
                "DELETE FROM capsules WHERE capsule_key = ?", [(key,) for key in stale]
            )
            conn.executemany(
                "INSERT OR REPLACE INTO capsules (capsule_key, signature, payload) "
                "VALUES (?, ?, ?)",
                [
                    (key, signature, _encode(capsule_calls, capsule_scores))
                    for key, (
                        signature,
                        capsule_calls,
                        capsule_scores,
                    ) in refreshed.items()
                ],
            )
            conn.commit()
        except sqlite3.Error as exc:
            logger.debug("query cache write failed (%s): cache left unchanged", exc)
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
    finally:
        _close(conn)


__all__ = [
    "CACHE_SCHEMA_VERSION",
    "INDEXER_SCHEMA_VERSION",
    "cache_db_path",
    "capsule_signature",
    "scan_capsule_dir_cached",
]
