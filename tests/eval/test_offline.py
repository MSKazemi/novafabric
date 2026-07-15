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

"""Tests for NF-009 trace-first zero-token offline eval (ADR-0099)."""

from __future__ import annotations

import json
from pathlib import Path

from novafabric.eval.offline import run_contract, run_coverage, run_metamorphic
from novafabric.eval.scores import ScoreSource, ScoreValueType


def _capsule(tmp_path: Path, tool_calls: list[dict] | None = None) -> Path:
    cap = tmp_path / "capsule"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: 01HXAY7M5JZ8R7K4P9DPBYK2WX\n")
    if tool_calls is not None:
        (cap / "tool-calls.jsonl").write_text(
            "\n".join(json.dumps(r) for r in tool_calls) + "\n"
        )
    return cap


# ── coverage (req 6) ─────────────────────────────────────────────────────────


def test_coverage_partial(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, [{"tool_name": "a"}, {"tool_name": "b"}])
    score = run_coverage(cap, ["a", "b", "c"])
    assert score.value == 2 / 3
    assert score.value_type is ScoreValueType.NUMERIC
    assert score.source is ScoreSource.CODE
    assert score.subject.startswith("sha256:")
    assert score.eval_card_digest.startswith("sha256:")
    assert score.evaluator_id == "nf-offline-coverage"


def test_coverage_full(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, [{"tool_name": "a"}, {"tool_name": "b"}])
    assert run_coverage(cap, ["a", "b"]).value == 1.0


def test_coverage_empty_declared_is_vacuous(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, [{"tool_name": "a"}])
    assert run_coverage(cap, []).value == 1.0


def test_coverage_no_tool_calls_file(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, None)
    assert run_coverage(cap, ["a"]).value == 0.0


# ── contract (req 6) ─────────────────────────────────────────────────────────

_SCHEMA = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}


def test_contract_all_valid(tmp_path: Path) -> None:
    cap = _capsule(
        tmp_path,
        [{"tool_name": "t", "output": {"ok": True}}, {"tool_name": "t", "output": {"ok": False}}],
    )
    assert run_contract(cap, _SCHEMA).value == 1.0


def test_contract_some_invalid(tmp_path: Path) -> None:
    cap = _capsule(
        tmp_path,
        [{"tool_name": "t", "output": {"ok": True}}, {"tool_name": "t", "output": {"bad": 1}}],
    )
    assert run_contract(cap, _SCHEMA).value == 0.5


def test_contract_missing_field_is_failure(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, [{"tool_name": "t"}])  # no 'output'
    assert run_contract(cap, _SCHEMA).value == 0.0


def test_contract_no_records_vacuous(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, None)
    score = run_contract(cap, _SCHEMA)
    assert score.value == 1.0
    assert score.evaluator_id == "nf-offline-contract"


# ── metamorphic (programmatic) ───────────────────────────────────────────────


def test_metamorphic_holds(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, None)
    score = run_metamorphic(cap, [(1, 2), (2, 4)], lambda a, b: b == 2 * a)
    assert score.value is True
    assert score.value_type is ScoreValueType.BOOLEAN
    assert score.evaluator_id == "nf-offline-metamorphic"


def test_metamorphic_violated(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, None)
    score = run_metamorphic(cap, [(1, 2), (2, 5)], lambda a, b: b == 2 * a)
    assert score.value is False


def test_metamorphic_empty_vacuous(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, None)
    assert run_metamorphic(cap, [], lambda a, b: False).value is True
