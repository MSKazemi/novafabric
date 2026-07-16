"""CLI tests for `nova graph agent` (ADR-0124)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from agent_graph.conftest import (
    CAPSULE_RUN_ID,
    CapsuleFactory,
    model_call,
    span,
    tool_call,
)
from novafabric.cli.main import app

runner = CliRunner()

MC = "01HXAY7M6FN9TQGE0V0M7PAY1Q"
TC = "01HXAY7M7QM4YZ2K7N9DPBYK2W"
SP = "9b2d04dac8e91f3a"


def _capsule(make_capsule: CapsuleFactory) -> Path:
    return make_capsule(
        spans=[span(SP, None)],
        model_calls=[model_call(MC, SP)],
        tool_calls=[tool_call(TC, SP, MC)],
    )


def test_graph_help() -> None:
    result = runner.invoke(app, ["graph", "--help"])
    assert result.exit_code == 0
    assert "agent" in result.output


def test_agent_json_default(make_capsule: CapsuleFactory) -> None:
    result = runner.invoke(app, ["graph", "agent", str(_capsule(make_capsule))])
    assert result.exit_code == 0
    document = json.loads(result.output)
    assert document["schema_version"] == "0.1.0"
    assert document["capsule_id"] == CAPSULE_RUN_ID
    assert {n["id"] for n in document["nodes"]} == {MC, TC, SP}
    assert document["graph_digest"].startswith("sha256:")


def test_agent_digest_flag(make_capsule: CapsuleFactory) -> None:
    capsule = _capsule(make_capsule)
    full = runner.invoke(app, ["graph", "agent", str(capsule)])
    digest = runner.invoke(app, ["graph", "agent", str(capsule), "--digest"])
    assert digest.exit_code == 0
    assert digest.output.strip() == json.loads(full.output)["graph_digest"]


def test_agent_stats_flag(make_capsule: CapsuleFactory) -> None:
    result = runner.invoke(
        app, ["graph", "agent", str(_capsule(make_capsule)), "--stats"]
    )
    assert result.exit_code == 0
    stats = json.loads(result.output)
    assert stats == {
        "edge_count": 3,
        "max_depth": 2,
        "max_fan_out": 2,
        "node_count": 3,
    }


def test_agent_mermaid_and_dot_formats(make_capsule: CapsuleFactory) -> None:
    capsule = _capsule(make_capsule)
    mermaid = runner.invoke(app, ["graph", "agent", str(capsule), "--format", "mermaid"])
    assert mermaid.exit_code == 0
    assert mermaid.output.startswith("graph TD")
    dot = runner.invoke(app, ["graph", "agent", str(capsule), "--format", "dot"])
    assert dot.exit_code == 0
    assert dot.output.startswith("digraph agent_execution_graph {")


def test_agent_output_file(make_capsule: CapsuleFactory, tmp_path: Path) -> None:
    target = tmp_path / "out" / "graph.json"
    target.parent.mkdir()
    result = runner.invoke(
        app, ["graph", "agent", str(_capsule(make_capsule)), "-o", str(target)]
    )
    assert result.exit_code == 0
    assert str(target) in result.output
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["capsule_id"] == CAPSULE_RUN_ID


def test_agent_rejects_unknown_format(make_capsule: CapsuleFactory) -> None:
    result = runner.invoke(
        app, ["graph", "agent", str(_capsule(make_capsule)), "--format", "svg"]
    )
    assert result.exit_code == 2
    assert "Graph error" in result.output


def test_agent_rejects_digest_with_stats(make_capsule: CapsuleFactory) -> None:
    result = runner.invoke(
        app, ["graph", "agent", str(_capsule(make_capsule)), "--digest", "--stats"]
    )
    assert result.exit_code == 2


def test_agent_missing_capsule_fails_cleanly(tmp_path: Path) -> None:
    result = runner.invoke(app, ["graph", "agent", str(tmp_path / "missing")])
    assert result.exit_code == 1
    assert "Graph error" in result.output
