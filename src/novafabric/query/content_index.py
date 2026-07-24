"""Capsule content full-text search index (ADR-0204 P1, experimental).

A persisted SQLite FTS5 index over the **redacted** text of local run
capsules, stored in the registry database beside ``runs_cache``. Same
contract as ``runs_cache``: a derived, rebuildable cache — never the source
of truth; per-run deletable; rebuilt from disk by ``nova search --reindex``.

Normative invariants (``design/spec/content-search-v0.md``):

1. **Corpus ⊆ secret-scan targets** — only files listed in
   :data:`novafabric.capture.secrets.SCAN_TARGETS` are ever indexed.
   :class:`StreamNotAllowedError` guards this at write time. Since ADR-0209
   the scanner covers more streams than the v0 corpus, so the corpus is
   **pinned** to :data:`_CORPUS` (a strict subset) rather than derived from
   ``SCAN_TARGETS``; indexing the extended event streams is ADR-0204 P2 and
   requires a ``corpus_rev`` bump.
2. **Post-redaction only** — a capsule is indexed only after ``capsule.yaml``
   exists, which every in-tree capture path writes *after*
   ``SecretScannerV0.scan_and_redact()`` has run.
3. **User input is data, never syntax** — :func:`build_match_expression`
   quotes every token as an FTS5 string literal; bare FTS5 operators in user
   input are matched literally.
4. **Size bounds** — per-doc, per-capsule, and per-source-file caps below.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novafabric.capture.secrets import SCAN_TARGETS

logger = logging.getLogger(__name__)

# ── constants (spec §"Size bounds", §"Snippet format") ───────────────────

CORPUS_REV = "v0"
DEFAULT_MAX_DOC_BYTES = 64 * 1024
DEFAULT_MAX_DOCS_PER_CAPSULE = 10_000
MAX_SOURCE_FILE_BYTES = 256 * 1024 * 1024
SNIPPET_TOKENS = 12
SNIPPET_MARKERS = ("«", "»")
SNIPPET_ELLIPSIS = "…"
MAX_MATCHES_PER_RUN = 8
_SCAN_ROW_CAP = 5000  # hard bound on doc-level rows examined per query

# The v0 corpus, deliberately PINNED (not derived from SCAN_TARGETS): ADR-0209
# extended the secret scanner to the extended event streams, but their corpus
# inclusion is gated on ADR-0204 P2 + a corpus_rev bump. Invariant 1 (corpus ⊆
# SCAN_TARGETS) is enforced below and pinned by tests/query/test_content_index.py.
_CORPUS: tuple[tuple[str, str], ...] = (
    ("model-calls.jsonl", "model-call-messages"),
    ("tool-calls.jsonl", "tool-call-arguments"),
    ("trace.jsonl", "trace"),
    ("capsule.yaml", "capsule-yaml"),
)

if not set(_CORPUS) <= set(SCAN_TARGETS):  # pragma: no cover — import-time guard
    raise RuntimeError(
        "content-index corpus must be a subset of the secret-scan targets "
        "(ADR-0204 invariant 1)"
    )

_ALLOWED_STREAMS: frozenset[str] = frozenset(kind for _, kind in _CORPUS)

_DDL = """
CREATE TABLE IF NOT EXISTS capsule_docs (
    doc_id   INTEGER PRIMARY KEY,
    run_id   TEXT NOT NULL,
    stream   TEXT NOT NULL,
    ref      TEXT NOT NULL,
    line_no  INTEGER,
    text     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_capsule_docs_run ON capsule_docs(run_id);

CREATE VIRTUAL TABLE IF NOT EXISTS capsule_fts USING fts5(
    text,
    content='capsule_docs',
    content_rowid='doc_id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS capsule_index_state (
    run_id     TEXT PRIMARY KEY,
    indexed_at TEXT NOT NULL,
    doc_count  INTEGER NOT NULL,
    skipped    INTEGER NOT NULL DEFAULT 0,
    corpus_rev TEXT NOT NULL
);
"""

FTS5_UNAVAILABLE_MSG = (
    "content search requires SQLite FTS5 (not available in this Python build)"
)


# ── errors ────────────────────────────────────────────────────────────────


class ContentSearchError(Exception):
    """Base error for the capsule content search subsystem."""


class ContentIndexUnavailableError(ContentSearchError):
    """The host SQLite build lacks the FTS5 extension."""


class StreamNotAllowedError(ContentSearchError):
    """Refusal to index a stream outside the secret-scan corpus (invariant 1)."""


class EmptyQueryError(ContentSearchError):
    """The query contains no searchable tokens."""


# ── availability / schema ─────────────────────────────────────────────────

_fts5_probe_result: bool | None = None


def fts5_available() -> bool:
    """True when this Python's ``sqlite3`` can create FTS5 tables (cached)."""
    global _fts5_probe_result  # noqa: PLW0603 — process-wide capability cache
    if _fts5_probe_result is None:
        probe = sqlite3.connect(":memory:")
        try:
            probe.execute("CREATE VIRTUAL TABLE __fts5_probe USING fts5(x)")
            _fts5_probe_result = True
        except sqlite3.OperationalError:
            _fts5_probe_result = False
        finally:
            probe.close()
    return _fts5_probe_result


def ensure_content_index(conn: sqlite3.Connection) -> None:
    """Create the content-index tables if absent. Idempotent, additive.

    Raises :class:`ContentIndexUnavailableError` when FTS5 is missing —
    metadata search (``runs_cache``) is unaffected either way.
    """
    if not fts5_available():
        raise ContentIndexUnavailableError(FTS5_UNAVAILABLE_MSG)
    conn.executescript(_DDL)


def content_index_enabled() -> bool:
    """False when ``NOVA_CONTENT_INDEX`` opts out of indexing at ingest."""
    return os.environ.get("NOVA_CONTENT_INDEX", "").strip().lower() not in {
        "off", "0", "false", "no",
    }


def _max_doc_bytes() -> int:
    raw = os.environ.get("NOVA_CONTENT_INDEX_MAX_DOC_KB")
    if raw:
        try:
            return max(1, int(raw)) * 1024
        except ValueError:
            pass
    return DEFAULT_MAX_DOC_BYTES


def _max_docs_per_capsule() -> int:
    raw = os.environ.get("NOVA_CONTENT_INDEX_MAX_DOCS")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return DEFAULT_MAX_DOCS_PER_CAPSULE


# ── extraction (spec §"Indexed fields", corpus_rev = "v0") ────────────────


@dataclass
class Doc:
    """One indexed text unit — a row of ``capsule_docs``."""

    run_id: str
    stream: str
    ref: str
    line_no: int | None
    text: str


@dataclass
class IndexResult:
    """Outcome of indexing one capsule."""

    run_id: str
    doc_count: int
    skipped: int


@dataclass
class ReindexStats:
    """Outcome of a :func:`reindex` sweep."""

    indexed: int = 0
    removed: int = 0
    skipped_docs: int = 0
    errors: list[str] = field(default_factory=list)


def _truncate_utf8(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def _strings_from_content(content: Any) -> list[str]:
    """String content of a message ``content`` field (str or parts list)."""
    if isinstance(content, str):
        return [content] if content else []
    out: list[str] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, str) and part:
                out.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str) and text:
                    out.append(text)
    return out


def _extract_model_call(record: dict[str, Any]) -> str | None:
    pieces: list[str] = []
    messages = record.get("gen_ai.request.messages")
    if isinstance(messages, list):
        for m in messages:
            if isinstance(m, dict):
                pieces.extend(_strings_from_content(m.get("content")))
    choices = record.get("gen_ai.response.choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if isinstance(message, dict):
                pieces.extend(_strings_from_content(message.get("content")))
            else:
                pieces.extend(_strings_from_content(choice.get("content")))
    return "\n".join(pieces) if pieces else None


def _extract_tool_call(record: dict[str, Any]) -> str | None:
    pieces: list[str] = []
    for key in ("tool_name", "name"):
        name = record.get(key)
        if isinstance(name, str) and name:
            pieces.append(name)
            break
    arguments = record.get("arguments")
    if isinstance(arguments, str) and arguments:
        pieces.append(arguments)
    elif arguments is not None:
        try:
            pieces.append(json.dumps(arguments, ensure_ascii=False, default=str))
        except (TypeError, ValueError):  # pragma: no cover — default=str covers
            pass
    for key in ("result", "output", "summary"):
        value = record.get(key)
        if isinstance(value, str) and value:
            pieces.append(value)
        elif isinstance(value, (dict, list)):
            try:
                pieces.append(json.dumps(value, ensure_ascii=False, default=str))
            except (TypeError, ValueError):  # pragma: no cover
                pass
    return "\n".join(pieces) if pieces else None


def _extract_trace(record: dict[str, Any]) -> str | None:
    pieces: list[str] = []
    name = record.get("name")
    if isinstance(name, str) and name:
        pieces.append(name)
    attributes = record.get("attributes")
    if isinstance(attributes, dict):
        pieces.extend(v for v in attributes.values() if isinstance(v, str) and v)
    return "\n".join(pieces) if pieces else None


def _extract_capsule_yaml(manifest: dict[str, Any]) -> str | None:
    pieces: list[str] = []
    command = manifest.get("command")
    if isinstance(command, list):
        joined = " ".join(str(c) for c in command)
        if joined:
            pieces.append(joined)
    elif isinstance(command, str) and command:
        pieces.append(command)
    tags = manifest.get("tags")
    if isinstance(tags, dict):
        for k, v in tags.items():
            pieces.append(str(k))
            if isinstance(v, str) and v:
                pieces.append(v)
    elif isinstance(tags, list):
        pieces.extend(str(t) for t in tags)
    metadata = manifest.get("metadata")
    if isinstance(metadata, dict):
        pieces.extend(v for v in metadata.values() if isinstance(v, str) and v)
    return "\n".join(pieces) if pieces else None


_JSONL_EXTRACTORS = {
    "model-call-messages": _extract_model_call,
    "tool-call-arguments": _extract_tool_call,
    "trace": _extract_trace,
}


def extract_docs(
    capsule_dir: Path,
    run_id: str,
) -> tuple[list[Doc], int]:
    """Extract the indexable docs of one capsule.

    Returns ``(docs, skipped)`` where ``skipped`` counts fully-dropped
    docs/files (malformed lines, over-cap docs, oversized source files) —
    truncated docs are *not* counted as skipped. Tolerant by design: a
    malformed line never raises out of this function.
    """
    max_doc_bytes = _max_doc_bytes()
    max_docs = _max_docs_per_capsule()
    docs: list[Doc] = []
    skipped = 0

    for filename, stream in _CORPUS:
        path = capsule_dir / filename
        if not path.exists() or not path.is_file():
            continue  # absent stream (e.g. minimal capture level) — not an error
        if path.stat().st_size > MAX_SOURCE_FILE_BYTES:
            logger.warning(
                "content index: %s/%s exceeds %d bytes — file skipped",
                run_id, filename, MAX_SOURCE_FILE_BYTES,
            )
            skipped += 1
            continue

        if filename == "capsule.yaml":
            import yaml  # noqa: PLC0415 — heavy import kept off module load

            try:
                manifest = yaml.safe_load(path.read_text()) or {}
            except (OSError, yaml.YAMLError):
                skipped += 1
                continue
            if not isinstance(manifest, dict):
                skipped += 1
                continue
            text = _extract_capsule_yaml(manifest)
            if text:
                if len(docs) >= max_docs:
                    skipped += 1
                else:
                    docs.append(Doc(
                        run_id=run_id, stream=stream, ref=filename,
                        line_no=None, text=_truncate_utf8(text, max_doc_bytes),
                    ))
            continue

        extractor = _JSONL_EXTRACTORS[stream]
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            skipped += 1
            continue
        for line_no, line in enumerate(lines, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(record, dict):
                skipped += 1
                continue
            text = extractor(record)
            if not text:
                continue  # record with no extractable text — no doc, not skipped
            if len(docs) >= max_docs:
                skipped += 1
                continue
            docs.append(Doc(
                run_id=run_id, stream=stream, ref=filename,
                line_no=line_no, text=_truncate_utf8(text, max_doc_bytes),
            ))
    return docs, skipped


# ── writes (indexer owns all writes; no triggers) ─────────────────────────


def _tables_present(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='capsule_docs'"
    ).fetchone()
    return row is not None


def _delete_run_rows(conn: sqlite3.Connection, run_id: str) -> None:
    """FTS5 external-content delete protocol + content/state row removal."""
    rows = conn.execute(
        "SELECT doc_id, text FROM capsule_docs WHERE run_id = ?", (run_id,)
    ).fetchall()
    for doc_id, text in rows:
        conn.execute(
            "INSERT INTO capsule_fts(capsule_fts, rowid, text)"
            " VALUES('delete', ?, ?)",
            (doc_id, text),
        )
    conn.execute("DELETE FROM capsule_docs WHERE run_id = ?", (run_id,))
    conn.execute("DELETE FROM capsule_index_state WHERE run_id = ?", (run_id,))


def index_docs(conn: sqlite3.Connection, run_id: str, docs: list[Doc], *,
               skipped: int = 0) -> IndexResult:
    """Replace ``run_id``'s rows with ``docs`` in one transaction.

    Enforces the corpus-subset invariant: any doc whose ``stream`` is not a
    secret-scanner ``target_kind`` raises :class:`StreamNotAllowedError`
    before any write happens. The ``capsule_index_state`` row is written
    last, so an interrupted index leaves an absent state row ⇒ "reindex me".
    """
    for doc in docs:
        if doc.stream not in _ALLOWED_STREAMS:
            raise StreamNotAllowedError(
                f"stream {doc.stream!r} is not a secret-scan target "
                f"(allowed: {sorted(_ALLOWED_STREAMS)}) — refusing to index "
                "unscanned content (ADR-0204 invariant 1)"
            )
    ensure_content_index(conn)
    with conn:
        _delete_run_rows(conn, run_id)
        for doc in docs:
            cur = conn.execute(
                "INSERT INTO capsule_docs(run_id, stream, ref, line_no, text)"
                " VALUES (?,?,?,?,?)",
                (doc.run_id, doc.stream, doc.ref, doc.line_no, doc.text),
            )
            conn.execute(
                "INSERT INTO capsule_fts(rowid, text) VALUES (?, ?)",
                (cur.lastrowid, doc.text),
            )
        conn.execute(
            "INSERT OR REPLACE INTO capsule_index_state"
            " (run_id, indexed_at, doc_count, skipped, corpus_rev)"
            " VALUES (?,?,?,?,?)",
            (
                run_id,
                datetime.now(timezone.utc).isoformat(),
                len(docs),
                skipped,
                CORPUS_REV,
            ),
        )
    return IndexResult(run_id=run_id, doc_count=len(docs), skipped=skipped)


def index_capsule(
    conn: sqlite3.Connection, capsule_dir: Path, run_id: str
) -> IndexResult:
    """Extract + (re)index one capsule. Idempotent delete-then-insert."""
    docs, skipped = extract_docs(capsule_dir, run_id)
    return index_docs(conn, run_id, docs, skipped=skipped)


def delete_run(conn: sqlite3.Connection, run_id: str) -> None:
    """Remove a run's content-index rows (erasure/GC contract). Idempotent."""
    if not _tables_present(conn):
        return
    with conn:
        _delete_run_rows(conn, run_id)


def maybe_index_capsule(
    conn: sqlite3.Connection, capsule_dir: Path, run_id: str
) -> None:
    """Fail-open ingest hook — never raises into the ingest pipeline.

    No-op when ``NOVA_CONTENT_INDEX=off`` or FTS5 is unavailable. Runs that
    already carry a current ``capsule_index_state`` row are skipped (capsules
    are immutable post-capture; ``--reindex --all`` forces re-extraction), so
    repeated full scans — e.g. serve-startup ``ingest_all`` — stay O(new
    capsules). A failure is logged and leaves the run without a state row, so
    the next ``--reindex`` repairs it; ``runs_cache`` population is never
    blocked.
    """
    if not content_index_enabled():
        return
    if not fts5_available():
        logger.debug("content index: FTS5 unavailable — skipping %s", run_id)
        return
    try:
        if _tables_present(conn):
            row = conn.execute(
                "SELECT corpus_rev FROM capsule_index_state WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is not None and row[0] == CORPUS_REV:
                return
    except sqlite3.Error:  # pragma: no cover — treat as "not indexed yet"
        pass
    try:
        index_capsule(conn, capsule_dir, run_id)
    except Exception as exc:  # noqa: BLE001 — fail-open by contract (D3)
        logger.warning("content index: indexing %s failed: %s", run_id, exc)


def reindex(
    conn: sqlite3.Connection,
    capsule_dir: Path,
    *,
    force: bool = False,
) -> ReindexStats:
    """Backfill/repair sweep over the capsule directory (idempotent).

    Indexes capsules without a current ``capsule_index_state`` row (all
    capsules when ``force``), and garbage-collects rows whose capsule
    directory no longer exists.
    """
    import yaml  # noqa: PLC0415

    from novafabric.serve.capsule_loader import (  # noqa: PLC0415
        discover_capsule_dirs,
        load_capsule_manifest,
    )

    ensure_content_index(conn)
    stats = ReindexStats()
    state_rows = conn.execute("SELECT run_id FROM capsule_index_state").fetchall()
    already = {r[0] for r in state_rows}

    present: set[str] = set()
    for d in discover_capsule_dirs(capsule_dir):
        try:
            m = load_capsule_manifest(d)
        except (FileNotFoundError, yaml.YAMLError):
            continue
        run_id = str(m.get("run_id", d.name))
        present.add(run_id)
        if not force and run_id in already:
            continue
        try:
            result = index_capsule(conn, d, run_id)
        except StreamNotAllowedError:
            raise
        except ContentSearchError as exc:
            stats.errors.append(f"{run_id}: {exc}")
            continue
        stats.indexed += 1
        stats.skipped_docs += result.skipped

    known = conn.execute(
        "SELECT run_id FROM capsule_index_state"
        " UNION SELECT DISTINCT run_id FROM capsule_docs"
    ).fetchall()
    for (run_id,) in known:
        if run_id not in present:
            delete_run(conn, run_id)
            stats.removed += 1
    return stats


# ── query (spec §"Query syntax and MATCH escaping") ───────────────────────


def build_match_expression(query: str) -> str:
    """Convert raw user input into a safe FTS5 MATCH expression.

    Every token becomes a quoted FTS5 string literal (internal ``"``
    doubled), AND-joined; a trailing ``*`` on a token (len > 1) becomes a
    prefix term with the ``*`` outside the quotes. Bare FTS5 operators
    arrive quoted and therefore match literally. Raises
    :class:`EmptyQueryError` for empty/whitespace-only input.
    """
    tokens = query.split()
    if not tokens:
        raise EmptyQueryError("empty query")
    parts: list[str] = []
    for tok in tokens:
        if tok.endswith("*") and len(tok) > 1:
            base = tok[:-1]
            parts.append('"' + base.replace('"', '""') + '"*')
        else:
            parts.append('"' + tok.replace('"', '""') + '"')
    return " ".join(parts)


@dataclass
class ContentMatch:
    """One doc-level hit inside a run."""

    stream: str
    ref: str
    line_no: int | None
    snippet: str


@dataclass
class RunContentHits:
    """All (capped) content matches for one run, best-rank order."""

    run_id: str
    created_at: str | None
    status: str | None
    matches: list[ContentMatch]
    matches_truncated: bool = False


def search_content(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
    since: str | None = None,
    until: str | None = None,
    status: str | None = None,
    stream: str | None = None,
    max_matches_per_run: int = MAX_MATCHES_PER_RUN,
) -> list[RunContentHits]:
    """FTS5 content search, grouped by run in bm25 rank order.

    ``since``/``until``/``status`` filter on the joined ``runs_cache``
    columns; ``stream`` restricts the corpus stream. Raises
    :class:`ContentIndexUnavailableError` without FTS5 and
    :class:`EmptyQueryError` for blank queries.
    """
    from novafabric.registry.runs_cache import ensure_runs_cache  # noqa: PLC0415

    ensure_content_index(conn)
    ensure_runs_cache(conn)
    match = build_match_expression(query)

    where = ["capsule_fts MATCH ?"]
    params: list[Any] = [match]
    if since:
        where.append("r.created_at >= ?")
        params.append(since)
    if until:
        where.append("r.created_at <= ?")
        params.append(until)
    if status and status != "all":
        where.append("r.status = ?")
        params.append(status)
    if stream:
        if stream not in _ALLOWED_STREAMS:
            raise StreamNotAllowedError(
                f"unknown stream {stream!r} (allowed: {sorted(_ALLOWED_STREAMS)})"
            )
        where.append("d.stream = ?")
        params.append(stream)

    sql = (
        "SELECT d.run_id, d.stream, d.ref, d.line_no,"
        f" snippet(capsule_fts, 0, '{SNIPPET_MARKERS[0]}', '{SNIPPET_MARKERS[1]}',"
        f" '{SNIPPET_ELLIPSIS}', {SNIPPET_TOKENS}) AS snip,"
        " r.created_at, r.status"
        " FROM capsule_fts"
        " JOIN capsule_docs d ON d.doc_id = capsule_fts.rowid"
        " LEFT JOIN runs_cache r ON r.run_id = d.run_id"
        f" WHERE {' AND '.join(where)}"
        " ORDER BY bm25(capsule_fts)"
        f" LIMIT {_SCAN_ROW_CAP}"
    )
    try:
        cursor = conn.execute(sql, params)
    except sqlite3.OperationalError as exc:  # defense in depth — quoted input
        raise ContentSearchError(f"content query failed: {exc}") from exc

    hits: dict[str, RunContentHits] = {}
    for run_id, doc_stream, ref, line_no, snip, created_at, run_status in cursor:
        entry = hits.get(run_id)
        if entry is None:
            if len(hits) >= limit:
                continue  # run cap reached; keep scanning to fill/truncate
            entry = RunContentHits(
                run_id=run_id, created_at=created_at, status=run_status,
                matches=[],
            )
            hits[run_id] = entry
        if len(entry.matches) >= max_matches_per_run:
            entry.matches_truncated = True
            continue
        entry.matches.append(
            ContentMatch(stream=doc_stream, ref=ref, line_no=line_no, snippet=snip)
        )
    return list(hits.values())
