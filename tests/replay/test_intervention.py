# tests/replay/test_intervention.py
"""Intervention (counterfactual) replay tests — ADR-0086, gap-005."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.diff._engine import DiffEngine
from novafabric.replay._engine import ReplayEngine
from novafabric.replay._flags import ReplayFlags
from novafabric.replay._intervention import (
    InterventionSpec,
    InterventionSpecError,
    InterventionTargetNotFoundError,
    apply_intervention,
    load_intervention_spec,
    run_checks,
)

GOLDEN_VALID_SPEC = {
    "target": {"stream": "model-calls", "event_index": 1},
    "replace_model_response": {"content": "DENY the request"},
    "checks": [
        {
            "name": "response-replaced",
            "stream": "model-calls",
            "event_index": 1,
            "field": "response.content",
            "contains": "DENY",
            "fatal": False,
        }
    ],
}

GOLDEN_INVALID_SPEC = {
    # two substitutions set — must be rejected
    "target": {"stream": "model-calls", "event_index": 0},
    "replace_model_response": {"content": "x"},
    "mutate_payload": {"y": 1},
}


def _model_calls() -> list[dict[str, object]]:
    return [
        {"span_id": "s0", "model": "m", "response": {"content": "step zero"}},
        {"span_id": "s1", "model": "m", "response": {"content": "APPROVE"}},
        {"span_id": "s2", "model": "m", "response": {"content": "final"}},
    ]


def _tool_calls() -> list[dict[str, object]]:
    return [{"span_id": "t0", "tool_name": "db", "result": {"rows": 3}}]


def _capsule(tmp_path: Path) -> Path:
    capsule = tmp_path / "run-int-1"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text(yaml.dump({"run_id": "run-int-1"}))
    (capsule / "model-calls.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in _model_calls())
    )
    (capsule / "tool-calls.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in _tool_calls())
    )
    return capsule


def _spec_file(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.dump(payload))
    return path


class TestSpec:
    def test_golden_valid_spec_loads(self, tmp_path: Path) -> None:
        spec = load_intervention_spec(_spec_file(tmp_path, GOLDEN_VALID_SPEC))
        assert spec.target.event_index == 1
        assert spec.checks[0].name == "response-replaced"

    def test_golden_invalid_spec_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(InterventionSpecError, match="exactly one"):
            load_intervention_spec(_spec_file(tmp_path, GOLDEN_INVALID_SPEC))

    def test_missing_file_and_bad_yaml(self, tmp_path: Path) -> None:
        with pytest.raises(InterventionSpecError):
            load_intervention_spec(None)
        bad = tmp_path / "bad.yaml"
        bad.write_text("{not: valid: yaml:")
        with pytest.raises(InterventionSpecError):
            load_intervention_spec(bad)

    def test_target_needs_exactly_one_selector(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            InterventionSpec.model_validate(
                {
                    "target": {"event_index": 1, "span_id": "s1"},
                    "mutate_payload": {"x": 1},
                }
            )

    def test_stream_substitution_mismatch_rejected(self) -> None:
        with pytest.raises(ValueError, match="tool-calls"):
            InterventionSpec.model_validate(
                {
                    "target": {"stream": "model-calls", "event_index": 0},
                    "replace_tool_result": {"rows": 0},
                }
            )


class TestApply:
    def test_replace_model_response_at_index(self) -> None:
        spec = InterventionSpec.model_validate(GOLDEN_VALID_SPEC)
        original = _model_calls()
        mutated, tools, idx = apply_intervention(spec, original, _tool_calls())
        assert idx == 1
        assert mutated[1]["response"] == {"content": "DENY the request"}
        assert mutated[1]["intervened"] is True
        # source untouched (read-only invariant)
        assert original[1]["response"] == {"content": "APPROVE"}

    def test_span_id_selector(self) -> None:
        spec = InterventionSpec.model_validate(
            {
                "target": {"stream": "tool-calls", "span_id": "t0"},
                "replace_tool_result": {"rows": 0},
            }
        )
        _, tools, idx = apply_intervention(spec, _model_calls(), _tool_calls())
        assert idx == 0
        assert tools[0]["result"] == {"rows": 0}

    @pytest.mark.parametrize("index", [0, 2])
    def test_first_and_last_event_intervenable(self, index: int) -> None:
        spec = InterventionSpec.model_validate(
            {
                "target": {"stream": "model-calls", "event_index": index},
                "mutate_payload": {"note": "edited"},
            }
        )
        mutated, _, idx = apply_intervention(spec, _model_calls(), _tool_calls())
        assert idx == index
        assert mutated[index]["note"] == "edited"

    def test_no_match_raises_named_error(self) -> None:
        for target in (
            {"stream": "model-calls", "event_index": 99},
            {"stream": "model-calls", "span_id": "nope"},
        ):
            spec = InterventionSpec.model_validate(
                {"target": target, "mutate_payload": {"x": 1}}
            )
            with pytest.raises(InterventionTargetNotFoundError):
                apply_intervention(spec, _model_calls(), _tool_calls())


class TestChecks:
    def test_check_outcomes_reported_by_name(self) -> None:
        spec = InterventionSpec.model_validate(GOLDEN_VALID_SPEC)
        mutated, tools, _ = apply_intervention(spec, _model_calls(), _tool_calls())
        outcomes = run_checks(spec.checks, mutated, tools)
        assert [(o.name, o.passed) for o in outcomes] == [
            ("response-replaced", True)
        ]

    def test_failing_check_never_swallowed(self) -> None:
        spec = InterventionSpec.model_validate(
            {
                **GOLDEN_VALID_SPEC,
                "checks": [
                    {
                        "name": "expects-approve",
                        "event_index": 1,
                        "field": "response.content",
                        "equals": "APPROVE",
                    }
                ],
            }
        )
        mutated, tools, _ = apply_intervention(spec, _model_calls(), _tool_calls())
        [outcome] = run_checks(spec.checks, mutated, tools)
        assert not outcome.passed
        assert "APPROVE" in outcome.detail

    def test_out_of_range_check_reports_failure(self) -> None:
        spec = InterventionSpec.model_validate(
            {
                **GOLDEN_VALID_SPEC,
                "checks": [
                    {
                        "name": "oob",
                        "event_index": 42,
                        "field": "x",
                        "equals": 1,
                    }
                ],
            }
        )
        mutated, tools, _ = apply_intervention(spec, _model_calls(), _tool_calls())
        [outcome] = run_checks(spec.checks, mutated, tools)
        assert not outcome.passed


class TestEngine:
    def _run(self, tmp_path: Path, spec_payload: dict[str, object]):  # type: ignore[no-untyped-def]
        capsule = _capsule(tmp_path)
        flags = ReplayFlags(
            mode="intervention",
            intervention_file=_spec_file(tmp_path, spec_payload),
        )
        base = tmp_path / "replays"
        engine = ReplayEngine(capsule_dir=capsule, flags=flags, base_dir=base)
        return capsule, base, engine.run()

    def test_engine_writes_marked_diffable_capsule(self, tmp_path: Path) -> None:
        capsule, base, result = self._run(tmp_path, GOLDEN_VALID_SPEC)
        assert result.mode == "intervention"
        assert result.status == "success"  # no command — streams emitted
        assert result.intervention is not None
        assert result.intervention["matched_event_index"] == 1
        out_dir = base / result.replay_id
        manifest = yaml.safe_load((out_dir / "capsule.yaml").read_text())
        assert manifest["replay_mode"] == "intervention"
        assert manifest["replay_of_run_id"] == "run-int-1"
        assert manifest["intervention"]["substitution"] == "replace_model_response"

    def test_output_diffable_with_structural_diff(self, tmp_path: Path) -> None:
        capsule, base, result = self._run(tmp_path, GOLDEN_VALID_SPEC)
        out_dir = base / result.replay_id
        report = DiffEngine().compare(capsule, out_dir)
        from novafabric.diff._format import format_json

        payload = json.loads(format_json(report))
        assert payload  # diff produced a structured report over the pair
        assert payload.get("run_b_id") == result.replay_id

    def test_fatal_check_aborts(self, tmp_path: Path) -> None:
        spec = {
            **GOLDEN_VALID_SPEC,
            "checks": [
                {
                    "name": "must-still-approve",
                    "event_index": 1,
                    "field": "response.content",
                    "equals": "APPROVE",
                    "fatal": True,
                }
            ],
        }
        _, _, result = self._run(tmp_path, spec)
        assert result.status == "aborted"
        assert result.error is not None
        assert "must-still-approve" in result.error["message"]

    def test_source_capsule_never_mutated(self, tmp_path: Path) -> None:
        capsule, _, _ = self._run(tmp_path, GOLDEN_VALID_SPEC)
        records = [
            json.loads(line)
            for line in (capsule / "model-calls.jsonl").read_text().splitlines()
        ]
        assert records[1]["response"] == {"content": "APPROVE"}
        assert "intervened" not in records[1]


class TestCli:
    def test_cli_intervention_round_trip(self, tmp_path: Path) -> None:
        from novafabric.cli.main import app

        capsule = _capsule(tmp_path)
        spec = _spec_file(tmp_path, GOLDEN_VALID_SPEC)
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "replay",
                str(capsule),
                "--mode",
                "intervention",
                "--intervention-file",
                str(spec),
                "--output-dir",
                str(tmp_path / "out"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "mode=intervention" in result.output

    def test_cli_requires_spec_file(self, tmp_path: Path) -> None:
        from novafabric.cli.main import app

        capsule = _capsule(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            app, ["replay", str(capsule), "--mode", "intervention"]
        )
        assert result.exit_code == 1
        assert "requires --intervention-file" in result.output

    def test_cli_no_match_exits_nonzero(self, tmp_path: Path) -> None:
        from novafabric.cli.main import app

        capsule = _capsule(tmp_path)
        spec = _spec_file(
            tmp_path,
            {
                "target": {"stream": "model-calls", "event_index": 99},
                "mutate_payload": {"x": 1},
            },
        )
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "replay",
                str(capsule),
                "--mode",
                "intervention",
                "--intervention-file",
                str(spec),
            ],
        )
        assert result.exit_code == 1
        assert "Intervention error" in result.output

    def test_default_mode_unchanged(self, tmp_path: Path) -> None:
        from novafabric.cli.main import app

        capsule = _capsule(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["replay", str(capsule), "--output-dir", str(tmp_path / "out"), "--dry-run"],
        )
        assert result.exit_code == 0, result.output
