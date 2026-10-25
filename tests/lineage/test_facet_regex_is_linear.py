"""Column-facet extraction is linear in the statement length (review replay-lineage-registry-11).

``select\\s+(.+?)\\s+from\\s+…`` backtracked quadratically: ``"select a" + " " * n + "x"``
took 0.4 s at n=5k and 5.6 s at n=20k, and a 100k-character tool argument (the cap)
about 140 s, per string, at capture finalize.
"""
from __future__ import annotations

import time

import pytest

from novafabric.lineage._facets import extract_column_facet


@pytest.mark.parametrize(
    "sql",
    [
        "select a" + " " * 90_000 + "x",
        "select a" + " " * 45_000 + "from" + " " * 45_000,
        "select " + "a, " * 30_000 + "b",
        "select a from" + " " * 90_000,
        "select\t" + "\n" * 90_000,
        "select a" + " from x" * 14_000,
    ],
)
def test_a_pathological_statement_finishes_fast(sql: str) -> None:
    started = time.perf_counter()
    extract_column_facet(sql)
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize(
    ("sql", "table", "columns"),
    [
        ("select a, b from t", "t", ["a", "b"]),
        ("SELECT  a ,b\n  FROM\n s.t WHERE x=1", "s.t", ["a", "b"]),
        ("select a\tfrom\tt", "t", ["a"]),
        ("select a, upper(b) from t", "t", ["a"]),
    ],
)
def test_ordinary_selects_still_parse(sql: str, table: str, columns: list[str]) -> None:
    facet = extract_column_facet(sql)
    assert facet is not None
    assert (facet.table, facet.columns, facet.access) == (table, columns, "read")


@pytest.mark.parametrize(
    "sql", ["select * from t", "select from t", "select a", "select a from", "select a from 1x"]
)
def test_unparseable_selects_still_return_none(sql: str) -> None:
    assert extract_column_facet(sql) is None
