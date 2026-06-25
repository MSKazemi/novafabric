"""Tests for the `nova diagnose <run-id>` CLI (ADR-0084).

Covers:
- text output ranks the responsible step and prints its taxonomy label;
- --output json emits a parseable RunAttribution;
- unknown run id exits non-zero with a clear message.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _failed_capsule(capsule_dir: Path, run_id: str) -> Path:
    cap = capsule_dir / run_id
    cap.mkdir(parents=True, exist_ok=True)
    cap_yaml = {
        "run_id": run_id,
        "status": "failure",
        "trace_ref": "trace.jsonl",
        "tool_calls_ref": "tool-calls.jsonl",
        "error": {"type": "ToolError", "message": "tool failed"},
    }
    (cap / "capsule.yaml").write_text(yaml.dump(cap_yaml))
    (cap / "trace.jsonl").write_text(
        json.dumps(
            {
                "span_id": "s1",
                "name": "tool.call",
                "started_at": "2026-06-11T10:00:00.000Z",
                "status": "error",
                "error": "tool call refused",
            }
        )
        + "\n"
    )
    (cap / "tool-calls.jsonl").write_text(
        json.dumps(
            {
                "span_id": "s1",
                "tool_name": "http_post",
                "status": "error",
                "started_at": "2026-06-11T10:00:00.000Z",
            }
        )
        + "\n"
    )
    return cap


class TestDiagnoseCmd:
    def test_text_output_ranks_responsible_step(self, tmp_path: Path) -> None:
        _failed_capsule(tmp_path, "run-1")
        result = runner.invoke(
            app, ["diagnose", "run-1", "--capsule-dir", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output
        assert "ACTION" in result.output
        assert "s1" in result.output

    def test_json_output_is_parseable(self, tmp_path: Path) -> None:
        _failed_capsule(tmp_path, "run-2")
        result = runner.invoke(
            app,
            ["diagnose", "run-2", "--capsule-dir", str(tmp_path), "--output", "json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["run_id"] == "run-2"
        assert payload["taxonomy"] == "ACTION"
        assert payload["responsible"]["step_id"] == "s1"
        assert isinstance(payload["ranked"], list)

    def test_unknown_run_exits_nonzero(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app, ["diagnose", "does-not-exist", "--capsule-dir", str(tmp_path)]
        )
        assert result.exit_code != 0
        assert "does-not-exist" in result.output
