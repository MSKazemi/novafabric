"""ADR-0128 — `nova validate --schemas` CLI (tool-call conformance report)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from novafabric.capture._ulid import new_ulid
from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.cli.main import app

runner = CliRunner()

ARGS_SCHEMA = {"type": "object", "required": ["query"],
               "properties": {"query": {"type": "string"}}}
RESULT_SCHEMA = {"type": "object", "required": ["ok"],
                 "properties": {"ok": {"type": "boolean"}}}


def _tool_call(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "0.1.0",
        "tool_call_id": new_ulid(),
        "parent_span_id": "0123456789abcdef",
        "started_at": "2026-07-15T10:00:00.000000Z",
        "finished_at": "2026-07-15T10:00:00.100000Z",
        "duration_ms": 100,
        "tool_name": "web_search",
        "tool_version": "unknown",
        "tool_provider": "shell://",
        "transport": "shell",
        "mutates": False,
        "mutation_class": "read-only",
        "arguments": {"query": "hello"},
        "arguments_schema_ref": "extensions/io.test/args.schema.json",
        "result": {"ok": True},
        "result_schema_ref": "extensions/io.test/result.schema.json",
        "status": "success",
        "agent_call_id": None,
    }
    base.update(overrides)
    return base


def _make_capsule(tmp_path: Path, records: list[dict[str, Any]]) -> Path:
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
    capsule_dir = orch.run(command=[sys.executable, "-c", "pass"]).capsule_dir
    ext = capsule_dir / "extensions" / "io.test"
    ext.mkdir(parents=True, exist_ok=True)
    (ext / "args.schema.json").write_text(json.dumps(ARGS_SCHEMA))
    (ext / "result.schema.json").write_text(json.dumps(RESULT_SCHEMA))
    (capsule_dir / "tool-calls.jsonl").write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records)
    )
    return capsule_dir


def test_validate_schemas_clean_capsule_reports_and_exits_zero(
    tmp_path: Path,
) -> None:
    capsule_dir = _make_capsule(tmp_path, [
        _tool_call(),
        _tool_call(arguments_schema_ref=None, result_schema_ref=None),
    ])
    result = runner.invoke(app, ["validate", "--schemas", str(capsule_dir)])
    assert result.exit_code == 0
    assert "Valid capsule" in result.output
    assert "schema-checked   : 1" in result.output
    assert "conformance: 2/2 checked payloads valid" in result.output


def test_validate_schemas_violation_is_reported_but_exit_zero(
    tmp_path: Path,
) -> None:
    capsule_dir = _make_capsule(tmp_path, [_tool_call(result={"ok": "yes"})])
    result = runner.invoke(app, ["validate", "--schemas", str(capsule_dir)])
    assert result.exit_code == 0  # report-only by default
    assert "1 violation(s)" in result.output
    assert "conformance: 1/2 checked payloads valid" in result.output


def test_validate_schemas_fail_on_violation_exits_nonzero(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path, [_tool_call(result={"ok": "yes"})])
    result = runner.invoke(
        app,
        ["validate", "--schemas", "--fail-on-schema-violation", str(capsule_dir)],
    )
    assert result.exit_code == 1


def test_validate_schemas_fail_flag_passes_on_clean_capsule(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path, [_tool_call()])
    result = runner.invoke(
        app,
        ["validate", "--schemas", "--fail-on-schema-violation", str(capsule_dir)],
    )
    assert result.exit_code == 0


def test_validate_schemas_unresolved_ref_is_visible_not_fatal(
    tmp_path: Path,
) -> None:
    capsule_dir = _make_capsule(tmp_path, [
        _tool_call(
            arguments_schema_ref="extensions/io.test/missing.schema.json",
            result_schema_ref=None,
        ),
    ])
    result = runner.invoke(
        app,
        ["validate", "--schemas", "--fail-on-schema-violation", str(capsule_dir)],
    )
    assert result.exit_code == 0  # null verdict is not a violation
    assert "unresolvable schema_ref" in result.output


def test_validate_schemas_write_backfills_verdicts(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path, [_tool_call()])
    result = runner.invoke(
        app, ["validate", "--schemas", "--write", str(capsule_dir)]
    )
    assert result.exit_code == 0
    assert "backfilled schema_validation verdicts on 1 record(s)" in result.output
    [line] = (capsule_dir / "tool-calls.jsonl").read_text().splitlines()
    record = json.loads(line)
    assert record["schema_validation"]["arguments_valid"] is True
    assert record["schema_validation"]["result_valid"] is True


def test_validate_without_schemas_flag_is_unchanged(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path, [_tool_call(result={"ok": "yes"})])
    result = runner.invoke(app, ["validate", str(capsule_dir)])
    assert result.exit_code == 0
    assert "conformance" not in result.output
    assert "Valid capsule" in result.output


def test_validate_schemas_on_non_capsule_target_errors(tmp_path: Path) -> None:
    spec = tmp_path / "spec.yaml"
    spec.write_text("name: x\n")
    result = runner.invoke(app, ["validate", "--schemas", str(spec)])
    assert result.exit_code == 2
    assert "requires a capsule directory" in result.output
