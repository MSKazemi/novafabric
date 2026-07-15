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

"""Tests for the NF-009 declarative metamorphic check-spec (ADR-0099).

The check-spec is validated against ``schemas/features/metamorphic-check-v0.schema.json``
and drives :func:`run_metamorphic_spec` — a zero-token structural check over a stored
capsule. Every test asserts the boolean Score and that no model call is made (pure I/O).
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema  # type: ignore[import-untyped]
import pytest

from novafabric.eval.offline import MetamorphicSpecError, run_metamorphic_spec
from novafabric.eval.scores import ScoreSource, ScoreValueType

SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "schemas"
        / "features"
        / "metamorphic-check-v0.schema.json"
    ).read_text(encoding="utf-8")
)


def _capsule(tmp_path: Path, records: list[dict], name: str = "tool-calls.jsonl") -> Path:
    cap = tmp_path / "capsule"
    cap.mkdir(exist_ok=True)
    (cap / "capsule.yaml").write_text("run_id: 01HXAY7M5JZ8R7K4P9DPBYK2WX\n")
    (cap / name).write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return cap


def _valid_spec(spec: dict) -> dict:
    jsonschema.Draft202012Validator(SCHEMA).validate(spec)
    return spec


# ── the check-spec schema itself is well-formed ──────────────────────────────


def test_schema_is_meta_valid() -> None:
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


def test_schema_requires_transform_and_invariant() -> None:
    v = jsonschema.Draft202012Validator(SCHEMA)
    assert not v.is_valid({"transform": "lower"})  # missing invariant
    assert not v.is_valid({"invariant": "equal"})  # missing transform
    assert v.is_valid({"transform": "lower", "invariant": "equal"})


# ── metamorphic consistency (the canonical NF-009 use) ───────────────────────


def test_normalized_inputs_with_equal_outputs_pass(tmp_path: Path) -> None:
    # Two inputs equal after lower+strip must produce the same output → invariant holds.
    cap = _capsule(
        tmp_path,
        [
            {"input": "Hello ", "output": "hi"},
            {"input": "hello", "output": "hi"},
        ],
    )
    spec = _valid_spec({"transform": ["lower", "strip"], "invariant": "equal"})
    score = run_metamorphic_spec(cap, spec)
    assert score.value is True
    assert score.value_type is ScoreValueType.BOOLEAN
    assert score.source is ScoreSource.CODE
    assert score.subject.startswith("sha256:")
    assert score.evaluator_id == "nf-offline-metamorphic"


def test_inconsistent_outputs_for_equivalent_inputs_fail(tmp_path: Path) -> None:
    cap = _capsule(
        tmp_path,
        [
            {"input": "Hello", "output": "hi"},
            {"input": "hello", "output": "HELLO THERE"},
        ],
    )
    spec = _valid_spec({"transform": "lower", "invariant": "equal"})
    assert run_metamorphic_spec(cap, spec).value is False


def test_distinct_inputs_never_paired(tmp_path: Path) -> None:
    # Different inputs (after transform) form separate groups → no pair → vacuously True.
    cap = _capsule(
        tmp_path,
        [
            {"input": "alpha", "output": "1"},
            {"input": "beta", "output": "2"},
        ],
    )
    spec = _valid_spec({"transform": "identity", "invariant": "equal"})
    assert run_metamorphic_spec(cap, spec).value is True


def test_single_string_transform_is_accepted(tmp_path: Path) -> None:
    cap = _capsule(
        tmp_path,
        [{"input": "A,b!", "output": "x"}, {"input": "ab", "output": "x"}],
    )
    spec = _valid_spec(
        {"transform": ["lower", "remove_punctuation"], "invariant": "equal"}
    )
    assert run_metamorphic_spec(cap, spec).value is True


# ── invariants ───────────────────────────────────────────────────────────────


def test_numeric_close_within_tolerance(tmp_path: Path) -> None:
    cap = _capsule(
        tmp_path,
        [
            {"input": "q", "output": 1.00},
            {"input": "q", "output": 1.02},
        ],
    )
    passing = _valid_spec(
        {"transform": "identity", "invariant": "numeric_close", "tolerance": 0.05}
    )
    failing = _valid_spec(
        {"transform": "identity", "invariant": "numeric_close", "tolerance": 0.01}
    )
    assert run_metamorphic_spec(cap, passing).value is True
    assert run_metamorphic_spec(cap, failing).value is False


def test_numeric_close_on_non_numeric_output_fails(tmp_path: Path) -> None:
    cap = _capsule(
        tmp_path,
        [{"input": "q", "output": "not-a-number"}, {"input": "q", "output": "x"}],
    )
    spec = _valid_spec({"transform": "identity", "invariant": "numeric_close"})
    assert run_metamorphic_spec(cap, spec).value is False


def test_length_within(tmp_path: Path) -> None:
    cap = _capsule(
        tmp_path,
        [{"input": "q", "output": "abcd"}, {"input": "q", "output": "abcde"}],
    )
    ok = _valid_spec(
        {"transform": "identity", "invariant": "length_within", "tolerance": 1}
    )
    bad = _valid_spec(
        {"transform": "identity", "invariant": "length_within", "tolerance": 0}
    )
    assert run_metamorphic_spec(cap, ok).value is True
    assert run_metamorphic_spec(cap, bad).value is False


def test_equal_normalized_ignores_case_and_whitespace(tmp_path: Path) -> None:
    cap = _capsule(
        tmp_path,
        [{"input": "q", "output": "Hello  World"}, {"input": "q", "output": "hello world"}],
    )
    spec = _valid_spec({"transform": "identity", "invariant": "equal_normalized"})
    assert run_metamorphic_spec(cap, spec).value is True


# ── custom fields, missing data, and errors ──────────────────────────────────


def test_custom_input_output_fields_and_records_file(tmp_path: Path) -> None:
    cap = _capsule(
        tmp_path,
        [{"prompt": "Hi", "answer": "42"}, {"prompt": "hi", "answer": "42"}],
        name="model-calls.jsonl",
    )
    spec = _valid_spec(
        {
            "transform": "lower",
            "invariant": "equal",
            "input_field": "prompt",
            "output_field": "answer",
            "records_file": "model-calls.jsonl",
        }
    )
    assert run_metamorphic_spec(cap, spec).value is True


def test_records_missing_fields_are_skipped(tmp_path: Path) -> None:
    cap = _capsule(
        tmp_path,
        [
            {"input": "q", "output": "a"},
            {"input": "q"},  # no output — skipped
            {"output": "b"},  # no input — skipped
        ],
    )
    spec = _valid_spec({"transform": "identity", "invariant": "equal"})
    # Only one usable record → no pair → vacuously True.
    assert run_metamorphic_spec(cap, spec).value is True


def test_no_records_file_is_vacuous(tmp_path: Path) -> None:
    cap = tmp_path / "empty"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: 01HXAY7M5JZ8R7K4P9DPBYK2WX\n")
    spec = {"transform": "identity", "invariant": "equal"}
    assert run_metamorphic_spec(cap, spec).value is True


def test_unknown_invariant_raises(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, [{"input": "q", "output": "a"}])
    with pytest.raises(MetamorphicSpecError):
        run_metamorphic_spec(cap, {"transform": "identity", "invariant": "nope"})


def test_missing_keys_raise(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, [{"input": "q", "output": "a"}])
    with pytest.raises(MetamorphicSpecError):
        run_metamorphic_spec(cap, {"transform": "identity"})
    with pytest.raises(MetamorphicSpecError):
        run_metamorphic_spec(cap, {"transform": [], "invariant": "equal"})


def test_custom_name_flows_through(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, [{"input": "q", "output": "a"}, {"input": "q", "output": "a"}])
    spec = {"transform": "identity", "invariant": "equal", "name": "paraphrase_consistency"}
    assert run_metamorphic_spec(cap, spec).name == "paraphrase_consistency"
