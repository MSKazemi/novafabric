"""Derived in-memory query index — DuckDB preferred, stdlib SQLite fallback.

ADR-0129 D2: the index is a *derived cache* built on demand from the capsule
directory; the signed capsules stay the source of truth. This first slice
builds the index in memory on every query (nothing is ever written to disk,
so the engine is read-only by construction; a persistent cache is future
work). The engine is selected internally — DuckDB (already a Tier-A runtime
dependency) when importable, stdlib ``sqlite3`` otherwise — and the query
semantics are identical either way: SQL is composed **only** from internal
column names and a fixed operator map; every user-supplied value is a bound
parameter. No user token ever reaches the SQL text.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from novafabric.query.indexer import IndexRows
from novafabric.query.model import LOG_LEVEL_RANKS, Predicate

logger = logging.getLogger(__name__)

_COLUMNS = (
    "run_id",
    "created_at",
    "status",
    "asset",
    "deployment_environment",
    "variant",
    "log_level",
    "log_level_rank",
    "tag",
    "model",
    "model_id",
)
_CALL_METRICS = ("cost", "prompt_tokens", "completion_tokens", "total_tokens", "latency")

#: Column name -> SQL type, in table order. Single source of truth: the CREATE
#: TABLE statements and the Arrow schema used by the bulk-load path are both
#: derived from these, so the two can never drift apart.
_DIM_COLUMNS: tuple[tuple[str, str], ...] = (
    ("run_id", "TEXT"),
    ("created_at", "DOUBLE"),
    ("status", "TEXT"),
    ("asset", "TEXT"),
    ("deployment_environment", "TEXT"),
    ("variant", "TEXT"),
    ("log_level", "TEXT"),
    ("log_level_rank", "INTEGER"),
    ("tag", "TEXT"),
    ("model", "TEXT"),
    ("model_id", "TEXT"),
)
_CALL_SCHEMA: tuple[tuple[str, str], ...] = _DIM_COLUMNS + (
    ("cost", "DOUBLE"),
    ("prompt_tokens", "DOUBLE"),
    ("completion_tokens", "DOUBLE"),
    ("total_tokens", "DOUBLE"),
    ("latency", "DOUBLE"),
)
_SCORE_SCHEMA: tuple[tuple[str, str], ...] = _DIM_COLUMNS + (
    ("name", "TEXT"),
    ("value", "DOUBLE"),
)


def _create_table(name: str, schema: tuple[tuple[str, str], ...]) -> str:
    body = ", ".join(f"{column} {sql_type}" for column, sql_type in schema)
    return f"CREATE TABLE {name} ({body})"


_CREATE_CALLS = _create_table("calls", _CALL_SCHEMA)
_CREATE_SCORES = _create_table("scores", _SCORE_SCHEMA)


class _Connection(Protocol):
    def execute(self, sql: str, parameters: Any = ..., /) -> Any: ...

    def executemany(self, sql: str, parameters: Any, /) -> Any: ...

    def close(self) -> None: ...

    # `register`/`unregister` are DuckDB-only (sqlite3.Connection has no
    # equivalent), so they stay off this Protocol and the two call sites in
    # _bulk_insert are guarded by an `engine == "duckdb"` check.


@dataclass(frozen=True)
class IndexInfo:
    """Provenance of the derived index, echoed in the result ``index`` block."""

    engine: str  # "duckdb" | "sqlite"
    built_at: str  # RFC 3339
    capsule_count: int


#: Opt back into DuckDB for the query index. See :func:`_detect_engine`.
_ENGINE_ENV_VAR = "NOVAFABRIC_QUERY_ENGINE"


def _detect_engine() -> str:
    """Pick the index backend. **SQLite by default, even when DuckDB is present.**

    This used to prefer DuckDB whenever it was importable, on the assumption
    that the `query` extra is an accelerator. Measured 2026-08-01 (ADR-0222
    OQ-3), it was ~20-25x *slower* at every size, because the index build bound
    a prepared INSERT row by row (~970 us/row).

    That defect is fixed: the build now uses DuckDB's columnar Arrow path
    (~1.1 us/row, see :func:`_bulk_insert`). Re-measured 2026-08-02 with
    `bench/query/bench_engine_crossover.py`:

        capsules   sqlite      duckdb     speedup
              10   0.0007 s    0.0169 s     0.04x
           1,000   0.0452 s    0.0529 s     0.86x
           5,000   0.2258 s    0.2304 s     0.98x
          20,000   0.9178 s    0.9137 s     1.00x

    **The default still stays SQLite**, for a different reason than before.
    DuckDB is no longer a trap, but it is not a win either: it reaches parity
    at ~20,000 capsules and never meaningfully passes SQLite, because the
    directory scan is **86-89% of total query time** at every size — the engine
    is ~3%. Choosing DuckDB buys a rounding error, and buys it only if pyarrow
    (~154 MB, larger than the whole default install) is present; without pyarrow
    it falls back to the slow row path and loses badly. SQLite needs no
    dependency at all and wins or ties everywhere measured.

    The rows are identical either way (pinned by
    `test_sqlite_and_duckdb_engines_return_identical_rows` and
    `test_engines_agree_on_a_mixed_batch`), so this stays purely a performance
    default. DuckDB remains reachable via ``run_query(..., engine="duckdb")``
    or ``NOVAFABRIC_QUERY_ENGINE=duckdb``.

    The next real win here is not the engine — it is not re-scanning the
    capsule directory on every query (a persistent index; ADR-0129 D2 calls it
    future work).
    """
    requested = os.environ.get(_ENGINE_ENV_VAR, "").strip().lower()
    if requested == "duckdb":
        try:
            import duckdb  # noqa: F401  (Tier-A, optional: `query`/`scale` extras)
        except ImportError:
            # An explicit request we cannot honour must not be silently
            # downgraded — but nor should it break a read-only query.
            logger.warning(
                "%s=duckdb but duckdb is not installed; using sqlite. "
                "Install it with: pip install 'novafabric[query]'",
                _ENGINE_ENV_VAR,
            )
            return "sqlite"
        return "duckdb"
    return "sqlite"


_SLOW_PATH_WARNED = False


def _bulk_insert(
    conn: _Connection,
    engine: str,
    table: str,
    schema: tuple[tuple[str, str], ...],
    params: list[tuple[Any, ...]],
) -> None:
    """Load ``params`` into ``table``, using each engine's fast path.

    SQLite's ``executemany`` is already the fast path. DuckDB's is **not**:
    binding a prepared INSERT row by row costs ~970 us/row (measured
    2026-08-02, `bench/query/`), which is what made the `query` extra ~20x
    *slower* than the stdlib fallback it was supposed to accelerate. Its
    columnar bulk path — register an Arrow table, then ``INSERT .. SELECT`` —
    costs ~1.1 us/row, roughly 880x better.

    Arrow is used only when ``pyarrow`` is already importable. It is **not** a
    dependency of the `query` extra on purpose: pyarrow is ~154 MB, larger than
    the entire 113 MB default install that ADR-0222 worked to achieve, and the
    measured end-to-end win over SQLite does not justify that (the directory
    scan dominates either way). Users who already have it — the `scale` and
    `serve` extras both pull it in — get the fast path for free; everyone else
    falls back to ``executemany`` with a one-time warning, because silently
    being 20x slower is exactly the failure this work exists to remove.
    """
    global _SLOW_PATH_WARNED
    if not params:
        # DuckDB's executemany rejects empty parameter sets.
        return

    columns = [name for name, _ in schema]
    insert_sql = f"INSERT INTO {table} VALUES ({', '.join('?' for _ in columns)})"

    if engine != "duckdb":
        conn.executemany(insert_sql, params)
        return

    try:
        import pyarrow as pa
    except ImportError:
        if not _SLOW_PATH_WARNED:
            _SLOW_PATH_WARNED = True
            logger.warning(
                "duckdb query index is using the slow row-by-row insert path "
                "(~970us/row); install pyarrow for the columnar path (~1.1us/row): "
                "pip install 'novafabric[scale]'. The default sqlite engine is "
                "unaffected and is faster than this fallback."
            )
        conn.executemany(insert_sql, params)
        return

    arrow_types = {
        "TEXT": pa.string(),
        "DOUBLE": pa.float64(),
        "INTEGER": pa.int32(),
    }
    # zip(*rows) transposes rows into columns; Arrow is columnar.
    column_values = list(zip(*params)) if params else [() for _ in columns]
    arrow_table = pa.table(
        {
            name: pa.array(list(values), type=arrow_types[sql_type])
            for (name, sql_type), values in zip(schema, column_values)
        }
    )
    view = f"_nova_bulk_{table}"
    conn.register(view, arrow_table)  # type: ignore[attr-defined]
    try:
        conn.execute(f"INSERT INTO {table} SELECT * FROM {view}")
    finally:
        # Never leave the scan target registered: it pins the Arrow buffers for
        # the life of the connection and would shadow a real table of that name.
        conn.unregister(view)  # type: ignore[attr-defined]


def _rank_of(level: str | None) -> int | None:
    if level is None:
        return None
    return LOG_LEVEL_RANKS.get(level)


def _build_where(
    predicates: tuple[Predicate, ...],
    since_epoch: float | None,
    until_epoch: float,
) -> tuple[str, list[Any]]:
    """Compose the WHERE clause from internal tokens only; values are bound."""
    clauses: list[str] = ["created_at < ?"]
    params: list[Any] = [until_epoch]
    if since_epoch is not None:
        clauses.append("created_at >= ?")
        params.append(since_epoch)
    for pred in predicates:
        column = pred.dimension
        if pred.op == "IN":
            placeholders = ", ".join("?" for _ in pred.values)
            clauses.append(f"{column} IN ({placeholders})")
            params.extend(pred.values)
        elif column == "log_level" and pred.op not in ("=", "!="):
            # Severity ordering (debug < info < warn < error), not lexicographic.
            clauses.append(f"log_level_rank {pred.op} ?")
            params.append(LOG_LEVEL_RANKS[str(pred.value)])
        else:
            clauses.append(f"{column} {pred.op} ?")
            params.append(pred.value)
    return " AND ".join(clauses), params


class QueryIndex:
    """An in-memory derived index over one capsule directory scan."""

    def __init__(self, conn: _Connection, engine: str, info: IndexInfo) -> None:
        self._conn = conn
        self.engine = engine
        self.info = info

    @classmethod
    def build(cls, rows: IndexRows, *, engine: str | None = None) -> "QueryIndex":
        """Build the index from scanned rows. ``engine`` forces a backend."""
        chosen = engine or _detect_engine()
        conn: _Connection
        if chosen == "duckdb":
            import duckdb

            conn = duckdb.connect(":memory:")
        elif chosen == "sqlite":
            conn = sqlite3.connect(":memory:")
        else:  # defensive: internal callers only pass known engines
            raise ValueError(f"unknown query index engine: {chosen!r}")
        conn.execute(_CREATE_CALLS)
        conn.execute(_CREATE_SCORES)
        # One bulk load per table. See _bulk_insert: SQLite uses executemany,
        # DuckDB uses its columnar Arrow path when pyarrow is available.
        call_params = [
            (
                    call.run_id,
                    call.created_at,
                    call.status,
                    call.asset,
                    call.deployment_environment,
                    call.variant,
                    call.log_level,
                    _rank_of(call.log_level),
                    call.tag,
                    call.model,
                    call.model_id,
                    call.cost,
                    call.prompt_tokens,
                    call.completion_tokens,
                    call.total_tokens,
                    call.latency,
            )
            for call in rows.calls
        ]
        _bulk_insert(conn, chosen, "calls", _CALL_SCHEMA, call_params)
        score_params = [
            (
                    score.run_id,
                    score.created_at,
                    score.status,
                    score.asset,
                    score.deployment_environment,
                    score.variant,
                    score.log_level,
                    _rank_of(score.log_level),
                    score.tag,
                    score.model,
                    score.model_id,
                    score.name,
                    score.value,
            )
            for score in rows.scores
        ]
        _bulk_insert(conn, chosen, "scores", _SCORE_SCHEMA, score_params)
        built_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        info = IndexInfo(engine=chosen, built_at=built_at, capsule_count=rows.capsule_count)
        return cls(conn, chosen, info)

    def _fetch(
        self,
        table: str,
        columns: tuple[str, ...],
        predicates: tuple[Predicate, ...],
        since_epoch: float | None,
        until_epoch: float,
    ) -> list[dict[str, Any]]:
        where_sql, params = _build_where(predicates, since_epoch, until_epoch)
        sql = f"SELECT {', '.join(columns)} FROM {table} WHERE {where_sql}"
        cursor = self._conn.execute(sql, params)
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def fetch_calls(
        self,
        predicates: tuple[Predicate, ...],
        since_epoch: float | None,
        until_epoch: float,
    ) -> list[dict[str, Any]]:
        """Filtered model-call rows (dims + metrics), aggregation-ready."""
        columns = _COLUMNS + _CALL_METRICS
        return self._fetch("calls", columns, predicates, since_epoch, until_epoch)

    def fetch_scores(
        self,
        predicates: tuple[Predicate, ...],
        since_epoch: float | None,
        until_epoch: float,
    ) -> list[dict[str, Any]]:
        """Filtered score rows (dims + name/value), aggregation-ready."""
        columns = _COLUMNS + ("name", "value")
        return self._fetch("scores", columns, predicates, since_epoch, until_epoch)

    def close(self) -> None:
        self._conn.close()
