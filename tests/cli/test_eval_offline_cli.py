# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""CLI tests for ``nova eval offline`` (NF-009, ADR-0099)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.eval.scores import read_scores

runner = CliRunner()


def _capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "capsule"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: 01HXAY7M5JZ8R7K4P9DPBYK2WX\n")
    (cap / "tool-calls.jsonl").write_text(
        '{"tool_name": "a", "output": {"ok": true}}\n'
        '{"tool_name": "b", "output": {"ok": false}}\n'
    )
    return cap


def test_coverage_check(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    result = runner.invoke(
        app,
        ["eval", "offline", "--capsule", str(cap), "--check", "coverage",
         "--declared-tools", "a,b,c"],
    )
    assert result.exit_code == 0, result.output
    assert "tool_coverage" in result.output


def test_coverage_emit_score_seals_into_capsule(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    result = runner.invoke(
        app,
        ["eval", "offline", "--capsule", str(cap), "--check", "coverage",
         "--declared-tools", "a,b", "--emit-score"],
    )
    assert result.exit_code == 0, result.output
    scores = read_scores(cap / "scores.jsonl")
    assert len(scores) == 1
    assert scores[0].name == "tool_coverage"
    assert scores[0].value == 1.0


def test_coverage_requires_declared_tools(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    result = runner.invoke(
        app, ["eval", "offline", "--capsule", str(cap), "--check", "coverage"]
    )
    assert result.exit_code == 2


def test_contract_check(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    schema_file = tmp_path / "schema.json"
    schema_file.write_text(
        json.dumps({"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}})
    )
    result = runner.invoke(
        app,
        ["eval", "offline", "--capsule", str(cap), "--check", "contract",
         "--schema", str(schema_file), "--json"],
    )
    assert result.exit_code == 0, result.output
    assert "output_contract" in result.output


def test_contract_requires_schema(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    result = runner.invoke(
        app, ["eval", "offline", "--capsule", str(cap), "--check", "contract"]
    )
    assert result.exit_code == 2


def test_not_a_capsule_dir(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["eval", "offline", "--capsule", str(tmp_path / "nope"), "--check", "coverage",
         "--declared-tools", "a"],
    )
    assert result.exit_code == 2


def test_offline_help_smoke() -> None:
    result = runner.invoke(app, ["eval", "offline", "--help"])
    assert result.exit_code == 0
    assert "coverage" in result.output


# ── metamorphic --spec (NF-009) ──────────────────────────────────────────────


def _metamorphic_capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "capsule"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: 01HXAY7M5JZ8R7K4P9DPBYK2WX\n")
    (cap / "tool-calls.jsonl").write_text(
        '{"input": "Hello ", "output": "hi"}\n'
        '{"input": "hello", "output": "hi"}\n'
    )
    return cap


def test_metamorphic_spec_passes(tmp_path: Path) -> None:
    cap = _metamorphic_capsule(tmp_path)
    spec = tmp_path / "check.yaml"
    spec.write_text("transform: [lower, strip]\ninvariant: equal\n")
    result = runner.invoke(
        app,
        ["eval", "offline", "--capsule", str(cap), "--check", "metamorphic",
         "--spec", str(spec)],
    )
    assert result.exit_code == 0, result.output
    assert "metamorphic" in result.output
    assert "True" in result.output


def test_metamorphic_spec_emit_score(tmp_path: Path) -> None:
    cap = _metamorphic_capsule(tmp_path)
    spec = tmp_path / "check.yaml"
    spec.write_text("transform: lower\ninvariant: equal\n")
    result = runner.invoke(
        app,
        ["eval", "offline", "--capsule", str(cap), "--check", "metamorphic",
         "--spec", str(spec), "--emit-score"],
    )
    assert result.exit_code == 0, result.output
    scores = read_scores(cap / "scores.jsonl")
    assert len(scores) == 1
    assert scores[0].name == "metamorphic"
    assert scores[0].value is True


def test_metamorphic_requires_spec(tmp_path: Path) -> None:
    cap = _metamorphic_capsule(tmp_path)
    result = runner.invoke(
        app, ["eval", "offline", "--capsule", str(cap), "--check", "metamorphic"]
    )
    assert result.exit_code == 2


def test_metamorphic_bad_spec_reports_usage_error(tmp_path: Path) -> None:
    cap = _metamorphic_capsule(tmp_path)
    spec = tmp_path / "check.yaml"
    spec.write_text("transform: identity\ninvariant: no_such_relation\n")
    result = runner.invoke(
        app,
        ["eval", "offline", "--capsule", str(cap), "--check", "metamorphic",
         "--spec", str(spec)],
    )
    assert result.exit_code == 2


def test_metamorphic_non_mapping_spec_errors(tmp_path: Path) -> None:
    cap = _metamorphic_capsule(tmp_path)
    spec = tmp_path / "check.yaml"
    spec.write_text("- just\n- a\n- list\n")
    result = runner.invoke(
        app,
        ["eval", "offline", "--capsule", str(cap), "--check", "metamorphic",
         "--spec", str(spec)],
    )
    assert result.exit_code == 2
