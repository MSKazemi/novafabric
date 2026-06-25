"""Tests for ClickHouse AggregatingMergeTree MV (cap-002, ADR-0066)."""

from __future__ import annotations

import datetime
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from novafabric.cost import clickhouse_store

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_client(query_rows: list[list] | None = None) -> MagicMock:
    client = MagicMock()
    result = MagicMock()
    result.result_rows = query_rows or []
    client.query.return_value = result
    client.command.return_value = None
    client.insert.return_value = None
    return client


# ---------------------------------------------------------------------------
# Tests: query_cost_report
# ---------------------------------------------------------------------------


class TestQueryCostReport:
    def test_returns_empty_when_no_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_CLICKHOUSE_URL", "http://localhost:8123/nova")
        client = _make_mock_client(query_rows=[])

        with patch("novafabric.cost.clickhouse_store._get_client", return_value=client):
            rows = clickhouse_store.query_cost_report(tenant_id="default", since_days=30)

        assert rows == []

    def test_returns_formatted_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_CLICKHOUSE_URL", "http://localhost:8123/nova")
        raw_rows = [
            ["gpt-4o", datetime.date(2026, 5, 1), 1.234567, 5000, 2500, 10],
            ["claude-3-5-sonnet", datetime.date(2026, 4, 30), 0.5, 2000, 1000, 5],
        ]
        client = _make_mock_client(query_rows=raw_rows)

        with patch("novafabric.cost.clickhouse_store._get_client", return_value=client):
            rows = clickhouse_store.query_cost_report(tenant_id="acme", since_days=7)

        assert len(rows) == 2
        first = rows[0]
        assert first["model_id"] == "gpt-4o"
        assert first["date"] == "2026-05-01"
        assert first["total_usd"] == round(1.234567, 6)
        assert first["prompt_tokens"] == 5000
        assert first["completion_tokens"] == 2500
        assert first["run_count"] == 10

    def test_raises_when_url_not_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NOVA_CLICKHOUSE_URL", raising=False)

        # _get_client() imports clickhouse_connect lazily before checking the URL,
        # so we stub the module to avoid ModuleNotFoundError.
        fake_cc = types.ModuleType("clickhouse_connect")
        fake_cc.get_client = MagicMock()  # type: ignore[attr-defined]

        with patch.dict(sys.modules, {"clickhouse_connect": fake_cc}):
            with pytest.raises(RuntimeError, match="NOVA_CLICKHOUSE_URL not set"):
                clickhouse_store.query_cost_report()

    def test_passes_correct_parameters(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """query_cost_report passes tenant_id and since_days to ClickHouse."""
        monkeypatch.setenv("NOVA_CLICKHOUSE_URL", "http://localhost:8123/nova")
        client = _make_mock_client(query_rows=[])

        with patch("novafabric.cost.clickhouse_store._get_client", return_value=client):
            clickhouse_store.query_cost_report(tenant_id="enterprise", since_days=90)

        client.query.assert_called_once()
        call_params = client.query.call_args.kwargs.get("parameters", {})
        assert call_params.get("tenant_id") == "enterprise"
        assert call_params.get("since_days") == 90

    def test_mv_query_uses_sum_merge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The MV query must use sumMerge/countMerge aggregation functions."""
        monkeypatch.setenv("NOVA_CLICKHOUSE_URL", "http://localhost:8123/nova")
        client = _make_mock_client(query_rows=[])

        with patch("novafabric.cost.clickhouse_store._get_client", return_value=client):
            clickhouse_store.query_cost_report()

        sql: str = client.query.call_args.args[0]
        assert "sumMerge" in sql
        assert "countMerge" in sql
        assert "cost_by_model_mv" in sql


# ---------------------------------------------------------------------------
# Tests: DDL contains AggregatingMergeTree
# ---------------------------------------------------------------------------


class TestDDLSchema:
    def test_ddl_contains_aggregating_merge_tree(self) -> None:
        """The cost MV DDL must use AggregatingMergeTree, not ReplacingMergeTree."""
        mv_ddl = clickhouse_store._DDL_COST_MV
        assert "AggregatingMergeTree" in mv_ddl
        assert "sumState" in mv_ddl
        assert "countState" in mv_ddl

    def test_raw_table_uses_merge_tree(self) -> None:
        """The raw cost_events table must use plain MergeTree."""
        events_ddl = clickhouse_store._DDL_COST_EVENTS
        assert "MergeTree" in events_ddl
        assert "ReplacingMergeTree" not in events_ddl

    def test_tenant_id_column_in_events_ddl(self) -> None:
        """The raw events table must include a tenant_id column."""
        assert "tenant_id" in clickhouse_store._DDL_COST_EVENTS
