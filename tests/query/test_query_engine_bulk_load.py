"""The DuckDB index build must use the columnar path, not row-by-row binding.

Binding a prepared INSERT row by row costs ~970 us/row in DuckDB, which is what
made the `query` extra ~20x *slower* than the stdlib fallback it exists to
accelerate (BL-026). The Arrow path costs ~1.1 us/row.

These tests assert the **mechanism** rather than wall-clock time: a timing
assertion would flake on a loaded CI box, and "fast enough today" is not the
property we care about. What we care about is that the row-by-row path is not
being taken when the columnar one is available — that is what regressed before
and what would regress again.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from novafabric.query import engine as engine_mod
from novafabric.query.engine import (
    _CALL_SCHEMA,
    _CREATE_CALLS,
    _SCORE_SCHEMA,
    QueryIndex,
    _bulk_insert,
    _create_table,
)
from novafabric.query.indexer import CallRow, IndexRows, ScoreRow

duckdb = pytest.importorskip("duckdb")


def _call(run_id: str = "r1", **over: Any) -> CallRow:
    base: dict[str, Any] = {
        "run_id": run_id,
        "created_at": 1.75e9,
        "status": "success",
        "asset": None,
        "deployment_environment": None,
        "variant": None,
        "log_level": "info",
        "tag": None,
        "model": "gpt-4o",
        "model_id": "gpt-4o",
        "cost": 0.01,
        "prompt_tokens": 100.0,
        "completion_tokens": 50.0,
        "total_tokens": 150.0,
        "latency": 300.0,
    }
    base.update(over)
    return CallRow(**base)


def _score(run_id: str = "r1", **over: Any) -> ScoreRow:
    base: dict[str, Any] = {
        "run_id": run_id,
        "created_at": 1.75e9,
        "status": "success",
        "asset": None,
        "deployment_environment": None,
        "variant": None,
        "log_level": "info",
        "tag": None,
        "model": "gpt-4o",
        "model_id": "gpt-4o",
        "name": "quality",
        "value": 0.9,
    }
    base.update(over)
    return ScoreRow(**base)


class _SpyConnection:
    """Wraps a real DuckDB connection and records which insert path was used."""

    def __init__(self) -> None:
        self._conn = duckdb.connect(":memory:")
        self.executemany_calls = 0
        self.registered: list[str] = []
        self.unregistered: list[str] = []

    def execute(self, sql: str, parameters: Any = None, /) -> Any:
        return self._conn.execute(sql) if parameters is None else self._conn.execute(sql, parameters)

    def executemany(self, sql: str, parameters: Any, /) -> Any:
        self.executemany_calls += 1
        return self._conn.executemany(sql, parameters)

    def register(self, name: str, obj: Any) -> Any:
        self.registered.append(name)
        return self._conn.register(name, obj)

    def unregister(self, name: str) -> Any:
        self.unregistered.append(name)
        return self._conn.unregister(name)

    def close(self) -> None:
        self._conn.close()


@pytest.fixture(autouse=True)
def _reset_warning_latch() -> Any:
    """`_bulk_insert` warns once per process; reset it between tests."""
    engine_mod._SLOW_PATH_WARNED = False
    yield
    engine_mod._SLOW_PATH_WARNED = False


def test_duckdb_uses_the_arrow_path_and_never_binds_row_by_row() -> None:
    pytest.importorskip("pyarrow")
    conn = _SpyConnection()
    conn.execute(_CREATE_CALLS)
    params = [tuple(getattr(_call(f"r{i}"), name, None) for name, _ in _CALL_SCHEMA) for i in range(5)]
    # log_level_rank is derived, not a CallRow attribute; fill it positionally.
    params = [tuple(1 if name == "log_level_rank" else v for (name, _), v in zip(_CALL_SCHEMA, row)) for row in params]

    _bulk_insert(conn, "duckdb", "calls", _CALL_SCHEMA, params)

    assert conn.executemany_calls == 0, "duckdb must not use the row-by-row path"
    assert conn.registered == ["_nova_bulk_calls"]
    assert conn.unregistered == ["_nova_bulk_calls"], "the Arrow view must not leak"
    assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 5
    conn.close()


def test_sqlite_still_uses_executemany() -> None:
    """SQLite's executemany *is* its fast path — do not change it."""
    import sqlite3

    class _SqliteSpy:
        """sqlite3.Connection's methods are read-only, so wrap rather than patch."""

        def __init__(self) -> None:
            self._conn = sqlite3.connect(":memory:")
            self.executemany_calls = 0

        def execute(self, sql: str, parameters: Any = None, /) -> Any:
            return self._conn.execute(sql) if parameters is None else self._conn.execute(sql, parameters)

        def executemany(self, sql: str, parameters: Any, /) -> Any:
            self.executemany_calls += 1
            return self._conn.executemany(sql, parameters)

        def close(self) -> None:
            self._conn.close()

    conn = _SqliteSpy()
    conn.execute(_CREATE_CALLS)
    _bulk_insert(conn, "sqlite", "calls", _CALL_SCHEMA, [tuple([None] * len(_CALL_SCHEMA))])
    assert conn.executemany_calls == 1
    assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 1
    conn.close()


def test_falls_back_to_executemany_without_pyarrow(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The `query` extra ships duckdb without pyarrow, so this path is real.

    It must still work — and must say so, because being silently 20x slower is
    the exact failure this work removes.
    """
    import builtins

    real_import = builtins.__import__

    def no_pyarrow(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "pyarrow":
            raise ImportError("simulated: pyarrow absent")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pyarrow)

    conn = _SpyConnection()
    conn.execute(_CREATE_CALLS)
    row = tuple(1 if name == "log_level_rank" else None for name, _ in _CALL_SCHEMA)
    with caplog.at_level(logging.WARNING, logger="novafabric.query.engine"):
        _bulk_insert(conn, "duckdb", "calls", _CALL_SCHEMA, [row])

    assert conn.executemany_calls == 1
    assert conn.registered == []
    assert "slow row-by-row insert path" in caplog.text
    assert "pyarrow" in caplog.text
    conn.close()


def test_slow_path_warning_is_emitted_once_per_process(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A per-row or per-query warning would be its own performance problem."""
    import builtins

    real_import = builtins.__import__

    def no_pyarrow(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "pyarrow":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pyarrow)
    row = tuple(1 if name == "log_level_rank" else None for name, _ in _CALL_SCHEMA)
    with caplog.at_level(logging.WARNING, logger="novafabric.query.engine"):
        for _ in range(3):
            conn = _SpyConnection()
            conn.execute(_CREATE_CALLS)
            _bulk_insert(conn, "duckdb", "calls", _CALL_SCHEMA, [row])
            conn.close()
    assert caplog.text.count("slow row-by-row insert path") == 1


def test_empty_batches_are_skipped() -> None:
    """DuckDB's executemany rejects empty parameter sets, and an empty Arrow
    table would be pointless work."""
    conn = _SpyConnection()
    conn.execute(_CREATE_CALLS)
    _bulk_insert(conn, "duckdb", "calls", _CALL_SCHEMA, [])
    assert conn.executemany_calls == 0 and conn.registered == []
    conn.close()


@pytest.mark.parametrize("engine", ["sqlite", "duckdb"])
def test_all_null_optional_columns_survive_the_round_trip(engine: str) -> None:
    """Arrow needs an explicit type per column; an all-NULL column is where a
    wrong or inferred type would surface (notably INTEGER log_level_rank)."""
    if engine == "duckdb":
        pytest.importorskip("pyarrow")
    rows = IndexRows(
        calls=[_call("r1", asset=None, tag=None, variant=None, log_level=None, cost=None)],
        scores=[_score("r1", asset=None, tag=None, variant=None, log_level=None, value=None)],
        capsule_count=1,
    )
    index = QueryIndex.build(rows, engine=engine)
    calls = index.fetch_calls((), None, 4.2e9)
    scores = index.fetch_scores((), None, 4.2e9)
    assert len(calls) == 1 and len(scores) == 1
    assert calls[0]["asset"] is None and calls[0]["log_level_rank"] is None
    assert calls[0]["cost"] is None and scores[0]["value"] is None
    index._conn.close()


def test_engines_agree_on_a_mixed_batch() -> None:
    """Parity is the invariant that lets the engine be a pure performance
    choice — if the two ever disagree, the default becomes a semantic one."""
    pytest.importorskip("pyarrow")
    rows = IndexRows(
        calls=[_call(f"r{i}", cost=float(i), log_level="warn" if i % 2 else "info") for i in range(20)],
        scores=[_score(f"r{i}", value=float(i) / 2) for i in range(20)],
        capsule_count=20,
    )
    out = {}
    for engine in ("sqlite", "duckdb"):
        index = QueryIndex.build(rows, engine=engine)
        out[engine] = (
            sorted(map(str, index.fetch_calls((), None, 4.2e9))),
            sorted(map(str, index.fetch_scores((), None, 4.2e9))),
        )
        index._conn.close()
    assert out["sqlite"] == out["duckdb"]


def test_ddl_is_generated_from_the_schema_tuples() -> None:
    """The CREATE TABLE text and the Arrow schema come from one source, so a
    column added to one cannot silently miss the other."""
    assert _create_table("calls", _CALL_SCHEMA) == _CREATE_CALLS
    assert [name for name, _ in _CALL_SCHEMA][:11] == [name for name, _ in _SCORE_SCHEMA][:11]
    assert {sql_type for _, sql_type in _CALL_SCHEMA + _SCORE_SCHEMA} <= {
        "TEXT",
        "DOUBLE",
        "INTEGER",
    }, "an unmapped SQL type would KeyError in the Arrow path"
