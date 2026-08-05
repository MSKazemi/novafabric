"""CLI tests for `nova query` (ADR-0129) — smoke, JSON contract, errors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from _help_assert import assert_flag_in_help
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

CapsuleFactory = Callable[..., Path]
RecordFactory = Callable[..., dict[str, Any]]


def test_query_help() -> None:
    result = runner.invoke(app, ["query", "--help"])
    assert result.exit_code == 0
    assert_flag_in_help(result, "--select")
    assert_flag_in_help(result, "--where")
    assert_flag_in_help(result, "--group-by")
    assert_flag_in_help(result, "--json")


def test_query_json_over_capsule_dir(
    make_capsule: CapsuleFactory, capsule_dir: Path, model_call: RecordFactory
) -> None:
    make_capsule(
        "run-1",
        metadata={"asset": "summarizer"},
        model_calls=[model_call(model="m1", cost=0.02)],
    )
    make_capsule(
        "run-2",
        metadata={"asset": "summarizer"},
        model_calls=[model_call(model="m1", cost=0.04)],
    )
    result = runner.invoke(
        app,
        [
            "query",
            "--select", "avg(cost) AS avg_cost, count() AS runs",
            "--where", "asset = summarizer",
            "--group-by", "model",
            "--capsule-dir", str(capsule_dir),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "0.1.0"
    assert payload["columns"] == ["model", "avg_cost", "runs"]
    assert payload["rows"] == [{"model": "m1", "avg_cost": 0.03, "runs": 2}]
    assert payload["row_count"] == 1
    assert payload["truncated"] is False
    assert payload["index"]["capsule_count"] == 2
    assert payload["index"]["engine"] in ("duckdb", "sqlite")


def test_query_table_output(
    make_capsule: CapsuleFactory, capsule_dir: Path, model_call: RecordFactory
) -> None:
    make_capsule("run-1", model_calls=[model_call(model="m1", cost=0.02)])
    result = runner.invoke(
        app,
        ["query", "--select", "count()", "--capsule-dir", str(capsule_dir)],
    )
    assert result.exit_code == 0, result.output
    assert "count()" in result.output
    assert "1 row(s)" in result.output
    assert "capsule(s) scanned" in result.output


def test_query_empty_dir_exits_zero(capsule_dir: Path) -> None:
    result = runner.invoke(
        app,
        ["query", "--select", "count()", "--capsule-dir", str(capsule_dir), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["rows"] == []
    assert payload["row_count"] == 0


def test_query_parse_error_exits_two(capsule_dir: Path) -> None:
    result = runner.invoke(
        app,
        ["query", "--select", "median(cost)", "--capsule-dir", str(capsule_dir)],
    )
    assert result.exit_code == 2
    assert "median" in result.output


def test_query_missing_select_exits_two(capsule_dir: Path) -> None:
    result = runner.invoke(app, ["query", "--capsule-dir", str(capsule_dir)])
    assert result.exit_code == 2
    assert "select" in result.output


def test_query_missing_capsule_dir_exits_one(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["query", "--select", "count()", "--capsule-dir", str(tmp_path / "nope")],
    )
    assert result.exit_code == 1
    assert "not found" in result.output


def test_query_file_with_flag_override(
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    model_call: RecordFactory,
    tmp_path: Path,
) -> None:
    make_capsule("run-1", model_calls=[model_call(model="m1", cost=0.02)])
    query_file = tmp_path / "q.yaml"
    query_file.write_text("select:\n  - count()\nwhere: model = m-none\n")
    # File alone matches nothing; the --where flag overrides the file's filter.
    result = runner.invoke(
        app,
        [
            "query",
            "--query-file", str(query_file),
            "--where", "model = m1",
            "--capsule-dir", str(capsule_dir),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["rows"] == [{"count()": 1}]
    assert payload["query"]["where"] == ["model = m1"]


def test_query_rebuild_index_flag_accepted(
    make_capsule: CapsuleFactory, capsule_dir: Path, model_call: RecordFactory
) -> None:
    make_capsule("run-1", model_calls=[model_call()])
    result = runner.invoke(
        app,
        [
            "query",
            "--select", "count()",
            "--capsule-dir", str(capsule_dir),
            "--rebuild-index",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["rows"] == [{"count()": 1}]
