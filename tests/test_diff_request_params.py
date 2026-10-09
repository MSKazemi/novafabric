"""A model call whose recorded request parameters changed is a changed call.

``nova diff`` compared a paired call's provider, model and messages, but not
the sampling parameters capture records beside them (``gen_ai.request.temperature``,
``top_p``, ``top_k``, ``max_tokens``, ``seed``, ``stop_sequences``,
``frequency_penalty``, ``presence_penalty``, ``choice.count``). A run whose
temperature went from 0 to 1 — or that lost its seed — was "No differences
found." and ``--assert-no-regressions`` exited 0 on it, though the change can
alter every response.

ADR-0303 Amendment 2: every recorded ``gen_ai.request.*`` attribute is part of
the request. A value that differs, or a key recorded on one side only, makes the
pair ``changed`` with ``request_changed``; additive ``params_changed`` and
``changed_params`` say which. Alignment is unchanged, so it is one changed pair,
not an added and a removed call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.diff._engine import DiffEngine

runner = CliRunner()


def _call(**extra: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "model_call_id": "m1",
        "parent_span_id": "root",
        "gen_ai.system": "openai",
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.request.messages": [{"role": "user", "content": "hi"}],
        "gen_ai.request.temperature": 0.0,
        "gen_ai.response.choices": [{"message": {"content": "hello"}}],
    }
    record.update(extra)
    return {k: v for k, v in record.items() if v is not None}


def _capsule(root: Path, run_id: str, *calls: dict[str, Any]) -> Path:
    d = root / run_id
    (d / "outputs").mkdir(parents=True)
    (d / "capsule.yaml").write_text(f"run_id: {run_id}\nstatus: success\n")
    (d / "model-calls.jsonl").write_text("".join(json.dumps(c) + "\n" for c in calls))
    (d / "tool-calls.jsonl").write_text("")
    return d


def _pair(tmp_path: Path, a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    report = DiffEngine().compare(_capsule(tmp_path, "run-a", a), _capsule(tmp_path, "run-b", b))
    assert len(report.model_call_pairs) == 1, report.model_call_pairs
    return report.model_call_pairs[0]


def test_a_changed_temperature_is_a_changed_request(tmp_path: Path) -> None:
    pair = _pair(tmp_path, _call(), _call(**{"gen_ai.request.temperature": 1.0}))
    assert pair["changed"] is True
    assert pair["request_changed"] is True
    assert pair["response_changed"] is False
    assert pair["provider_changed"] is False
    assert pair["params_changed"] is True
    assert pair["changed_params"] == ["gen_ai.request.temperature"]


def test_a_parameter_recorded_on_one_side_only_is_a_change(tmp_path: Path) -> None:
    """Absent is not equal to any value: a lost seed is a changed request."""
    pair = _pair(
        tmp_path,
        _call(**{"gen_ai.request.seed": 42, "gen_ai.request.max_tokens": 64}),
        _call(**{"gen_ai.request.max_tokens": 64}),
    )
    assert pair["changed"] is True
    assert pair["changed_params"] == ["gen_ai.request.seed"]


def test_several_changed_parameters_are_listed_sorted(tmp_path: Path) -> None:
    pair = _pair(
        tmp_path,
        _call(**{"gen_ai.request.top_p": 0.9, "gen_ai.request.stop_sequences": ["\n"]}),
        _call(**{
            "gen_ai.request.top_p": 0.5,
            "gen_ai.request.stop_sequences": ["END"],
            "gen_ai.request.temperature": 0.7,
        }),
    )
    assert pair["changed_params"] == [
        "gen_ai.request.stop_sequences",
        "gen_ai.request.temperature",
        "gen_ai.request.top_p",
    ]


def test_equal_parameters_are_unchanged(tmp_path: Path) -> None:
    params = {"gen_ai.request.seed": 7, "gen_ai.request.max_tokens": 64}
    pair = _pair(tmp_path, _call(**params), _call(**params))
    assert pair["changed"] is False
    assert pair["params_changed"] is False
    assert pair["changed_params"] == []


def test_model_and_messages_are_not_listed_as_parameters(tmp_path: Path) -> None:
    """They are compared already, and stay under request_changed alone."""
    pair = _pair(tmp_path, _call(), _call(**{"gen_ai.request.model": "gpt-4o-mini"}))
    assert pair["request_changed"] is True
    assert pair["params_changed"] is False
    assert pair["changed_params"] == []


def test_response_and_usage_attributes_are_not_request_parameters(tmp_path: Path) -> None:
    pair = _pair(
        tmp_path,
        _call(**{"gen_ai.response.id": "r1", "gen_ai.usage.input_tokens": 5, "duration_ms": 10}),
        _call(**{"gen_ai.response.id": "r2", "gen_ai.usage.input_tokens": 6, "duration_ms": 99}),
    )
    assert pair["changed"] is False


def test_gate_exits_1_on_a_parameter_only_change(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "run-a", _call())
    b = _capsule(tmp_path, "run-b", _call(**{"gen_ai.request.temperature": 1.0}))
    result = runner.invoke(app, ["diff", str(a), str(b), "--assert-no-regressions"])
    assert result.exit_code == 1, result.output
    doc = json.loads(
        runner.invoke(app, ["diff", str(a), str(b), "--output-format", "json"]).stdout
    )
    assert doc["has_changes"] is True
    pair = doc["sections"]["model_calls"]["pairs"][0]
    assert pair["changed_params"] == ["gen_ai.request.temperature"]
