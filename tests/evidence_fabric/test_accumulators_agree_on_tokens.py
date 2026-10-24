"""The DuckDB and ClickHouse accumulators record the same model and token count for one event.

``POST /capsule`` (``collector_app.CapsuleEvent``) accepts both spellings — ``model`` /
``tokens_in`` / ``tokens_out`` (the DuckDB columns) and ``model_id`` / ``tokens_total`` (the
ClickHouse ones) — but each accumulator read only its own: an event sent as
``tokens_in``/``tokens_out`` to a ClickHouse-backed collector stored ``tokens_total = NULL``,
and one sent with ``model_id`` to a DuckDB-backed collector stored no model at all, so its
call vanished from the cost report. One property over both implementations, asserted once.

``tokens_total`` alone cannot be split into input and output, so the DuckDB projection (which
has no total column) does not invent a split; that case is ClickHouse-only and pinned here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from novafabric.evidence_fabric.clickhouse_accumulator import ClickHouseAccumulator  # noqa: E402
from novafabric.evidence_fabric.duckdb_accumulator import DuckDBAccumulator  # noqa: E402

_BASE = {"run_id": "r1", "event_type": "model_call", "tenant": "default"}


def _clickhouse_row(event: dict[str, Any]) -> tuple[str | None, int | None]:
    client = MagicMock()
    ClickHouseAccumulator(client=client).ingest_events([{**_BASE, **event}])
    (_, rows), kwargs = client.insert.call_args
    row = dict(zip(kwargs["column_names"], rows[0], strict=True))
    return row["model_id"], row["tokens_total"]


def _duckdb_row(event: dict[str, Any], tmp_path: Path) -> tuple[str, int]:
    with DuckDBAccumulator(tmp_path / "e.duckdb") as acc:
        acc.ingest_capsule_events([{**_BASE, **event}])
        model, tok_in, tok_out = acc._conn.execute(
            "SELECT model, tokens_in, tokens_out FROM capsule_events"
        ).fetchone()
    return model, tok_in + tok_out


@pytest.mark.parametrize(
    "event",
    [
        {"model": "gpt-4o", "tokens_in": 10, "tokens_out": 5},
        {"model_id": "gpt-4o", "tokens_in": 10, "tokens_out": 5},
        {"model": "gpt-4o", "model_id": "gpt-4o", "tokens_in": 15},
        {"model_id": "gpt-4o", "tokens_out": 15},
    ],
)
def test_both_accumulators_record_the_same_model_and_tokens(
    event: dict[str, Any], tmp_path: Path
) -> None:
    assert _clickhouse_row(event) == ("gpt-4o", 15)
    assert _duckdb_row(event, tmp_path) == ("gpt-4o", 15)


def test_explicit_tokens_total_wins_in_clickhouse() -> None:
    event = {"model_id": "m", "tokens_total": 42, "tokens_in": 1, "tokens_out": 1}
    assert _clickhouse_row(event) == ("m", 42)


def test_no_token_fields_stays_unknown_in_clickhouse() -> None:
    # NULL means "not reported", which SUM() skips; 0 would claim a measured zero.
    assert _clickhouse_row({"model_id": "m"}) == ("m", None)


def test_duckdb_does_not_split_a_bare_total(tmp_path: Path) -> None:
    assert _duckdb_row({"model_id": "m", "tokens_total": 42}, tmp_path) == ("m", 0)
