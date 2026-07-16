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

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from novafabric.query.indexer import IndexRows
from novafabric.query.model import LOG_LEVEL_RANKS, Predicate

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

_CREATE_CALLS = (
    "CREATE TABLE calls ("
    "run_id TEXT, created_at DOUBLE, status TEXT, asset TEXT, "
    "deployment_environment TEXT, variant TEXT, log_level TEXT, "
    "log_level_rank INTEGER, tag TEXT, model TEXT, model_id TEXT, "
    "cost DOUBLE, prompt_tokens DOUBLE, completion_tokens DOUBLE, "
    "total_tokens DOUBLE, latency DOUBLE)"
)
_CREATE_SCORES = (
    "CREATE TABLE scores ("
    "run_id TEXT, created_at DOUBLE, status TEXT, asset TEXT, "
    "deployment_environment TEXT, variant TEXT, log_level TEXT, "
    "log_level_rank INTEGER, tag TEXT, model TEXT, model_id TEXT, "
    "name TEXT, value DOUBLE)"
)


class _Connection(Protocol):
    def execute(self, sql: str, parameters: Any = ..., /) -> Any: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class IndexInfo:
    """Provenance of the derived index, echoed in the result ``index`` block."""

    engine: str  # "duckdb" | "sqlite"
    built_at: str  # RFC 3339
    capsule_count: int


def _detect_engine() -> str:
    try:
        import duckdb  # noqa: F401  (Tier-A, already a runtime dep)
    except ImportError:  # pragma: no cover - duckdb is a default dependency
        return "sqlite"
    return "duckdb"


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
        call_sql = f"INSERT INTO calls VALUES ({', '.join('?' for _ in range(16))})"
        for call in rows.calls:
            conn.execute(
                call_sql,
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
                ),
            )
        score_sql = f"INSERT INTO scores VALUES ({', '.join('?' for _ in range(13))})"
        for score in rows.scores:
            conn.execute(
                score_sql,
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
                ),
            )
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
