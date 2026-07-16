"""Tests for `nova cost report` CLI command (cap-002, ADR-0066)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from novafabric.cli.cost import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_query_cost_report(rows):
    """Patch query_cost_report to return fixed rows.

    The function is imported lazily inside the CLI command from
    novafabric.cost.clickhouse_store, so we patch it there.
    """
    return patch(
        "novafabric.cost.clickhouse_store.query_cost_report",
        return_value=rows,
    )


SAMPLE_ROWS = [
    {
        "model_id": "gpt-4o",
        "date": "2026-05-01",
        "total_usd": 1.25,
        "prompt_tokens": 5000,
        "completion_tokens": 2500,
        "run_count": 10,
    },
    {
        "model_id": "claude-3-5-sonnet",
        "date": "2026-04-30",
        "total_usd": 0.75,
        "prompt_tokens": 2000,
        "completion_tokens": 1000,
        "run_count": 5,
    },
]


# ---------------------------------------------------------------------------
# Tests: missing NOVA_CLICKHOUSE_URL
# ---------------------------------------------------------------------------


class TestCostReportMissingURL:
    def test_exits_with_error_when_url_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NOVA_CLICKHOUSE_URL", raising=False)
        # `nova cost` grew a second command (estimate, ADR-0133); name report explicitly.
        result = runner.invoke(app, ["report"])
        assert result.exit_code != 0
        assert "NOVA_CLICKHOUSE_URL" in result.output or "NOVA_CLICKHOUSE_URL" in (
            result.stderr or ""
        )


# ---------------------------------------------------------------------------
# Tests: table format
# ---------------------------------------------------------------------------


class TestCostReportTableFormat:
    def test_table_format_contains_model_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVA_CLICKHOUSE_URL", "http://localhost:8123/nova")

        with _mock_query_cost_report(SAMPLE_ROWS):
            result = runner.invoke(app, ["report", "--format", "table"])

        assert result.exit_code == 0
        assert "gpt-4o" in result.output
        assert "claude-3-5-sonnet" in result.output

    def test_table_format_contains_headers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVA_CLICKHOUSE_URL", "http://localhost:8123/nova")

        with _mock_query_cost_report(SAMPLE_ROWS):
            result = runner.invoke(app, ["report"])

        assert result.exit_code == 0
        assert "model_id" in result.output
        assert "total_usd" in result.output

    def test_empty_rows_shows_no_data_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_CLICKHOUSE_URL", "http://localhost:8123/nova")

        with _mock_query_cost_report([]):
            result = runner.invoke(app, ["report"])

        assert result.exit_code == 0
        assert "No cost data" in result.output


# ---------------------------------------------------------------------------
# Tests: JSON format
# ---------------------------------------------------------------------------


class TestCostReportJsonFormat:
    def test_json_output_is_valid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVA_CLICKHOUSE_URL", "http://localhost:8123/nova")

        with _mock_query_cost_report(SAMPLE_ROWS):
            result = runner.invoke(app, ["report", "--format", "json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["model_id"] == "gpt-4o"

    def test_json_output_empty_is_empty_array(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_CLICKHOUSE_URL", "http://localhost:8123/nova")

        with _mock_query_cost_report([]):
            result = runner.invoke(app, ["report", "--format", "json"])

        assert result.exit_code == 0
        assert json.loads(result.output) == []


# ---------------------------------------------------------------------------
# Tests: CSV format
# ---------------------------------------------------------------------------


class TestCostReportCsvFormat:
    def test_csv_output_has_header_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVA_CLICKHOUSE_URL", "http://localhost:8123/nova")

        with _mock_query_cost_report(SAMPLE_ROWS):
            result = runner.invoke(app, ["report", "--format", "csv"])

        assert result.exit_code == 0
        lines = result.output.strip().splitlines()
        # First line is header
        assert lines[0] == "date,model_id,total_usd,prompt_tokens,completion_tokens,run_count"

    def test_csv_output_data_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVA_CLICKHOUSE_URL", "http://localhost:8123/nova")

        with _mock_query_cost_report(SAMPLE_ROWS):
            result = runner.invoke(app, ["report", "--format", "csv"])

        assert result.exit_code == 0
        lines = result.output.strip().splitlines()
        assert len(lines) == 3  # header + 2 data rows
        assert "gpt-4o" in lines[1]

    def test_csv_empty_rows_no_data_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_CLICKHOUSE_URL", "http://localhost:8123/nova")

        with _mock_query_cost_report([]):
            result = runner.invoke(app, ["report", "--format", "csv"])

        assert result.exit_code == 0
        # Empty data produces empty string
        assert result.output.strip() == ""


# ---------------------------------------------------------------------------
# Tests: CLI options
# ---------------------------------------------------------------------------


class TestCostReportOptions:
    def test_tenant_option_passed_to_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_CLICKHOUSE_URL", "http://localhost:8123/nova")

        with _mock_query_cost_report([]) as mock_fn:
            runner.invoke(app, ["report", "--tenant", "acme"])

        mock_fn.assert_called_once()
        assert mock_fn.call_args.kwargs.get("tenant_id") == "acme"

    def test_since_days_option_passed_to_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_CLICKHOUSE_URL", "http://localhost:8123/nova")

        with _mock_query_cost_report([]) as mock_fn:
            runner.invoke(app, ["report", "--since-days", "90"])

        mock_fn.assert_called_once()
        assert mock_fn.call_args.kwargs.get("since_days") == 90

    def test_invalid_format_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_CLICKHOUSE_URL", "http://localhost:8123/nova")

        result = runner.invoke(app, ["report", "--format", "xml"])
        assert result.exit_code != 0
