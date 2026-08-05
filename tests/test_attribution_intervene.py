"""Tests for intervention-verified attribution — ADR-0101 first slice (NF-017/NF-022).

Covers:
- CONFIRMED: the auto-synthesized intervention flips a real re-executed failure
  to success (SPK-ATTR-1 fixture: the fix is known).
- REFUTED: the re-execution still fails after the intervention.
- INCONCLUSIVE (honest, evidence-based — never guessed):
  * the hypothesis class is not auto-mappable (tool/span/run steps);
  * the implicated model step cannot be located in the model-calls stream;
  * the capsule has no re-executable command (flip not measurable);
  * attribution produced no hypothesis;
  * the original run did not fail;
  * defensive: replay aborted / named intervention error.
- CLI: --intervene emits the verification block (JSON + text); default output
  is unchanged without the flag; missing capsule exits 1; --help shows the flag.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from _help_assert import assert_flag_in_help
from typer.testing import CliRunner

from novafabric.diagnose import attribute_failure
from novafabric.diagnose.verify import (
    UnmappableHypothesisError,
    Verdict,
    synthesize_intervention_payload,
    verify_hypothesis,
)

runner = CliRunner()

# Simulates an agent whose success depends on the replayed model-call queue:
# exits 1 while any queued model call still carries an error signal.
_CHECK_QUEUE_SCRIPT = (
    "import json, os, sys\n"
    "queue = json.loads(open(os.environ['NOVAFABRIC_REPLAY_QUEUE_PATH']).read())\n"
    "sys.exit(1 if any(r.get('error') for r in queue) else 0)\n"
)

# Simulates a failure the hypothesis does NOT explain: correcting the
# implicated step cannot flip the outcome.
_ALWAYS_FAIL_SCRIPT = "import sys\nsys.exit(1)\n"


def _erroring_model_calls() -> list[dict[str, object]]:
    return [
        {"span_id": "m0", "model": "gpt", "status": "ok"},
        {
            "span_id": "m1",
            "model": "gpt",
            "status": "error",
            "error": "model call rejected",
        },
    ]


def _write_capsule(
    root: Path,
    run_id: str,
    *,
    status: str = "failure",
    command: list[str] | None = None,
    trace: list[dict] | None = None,
    tool_calls: list[dict] | None = None,
    model_calls: list[dict] | None = None,
    error: dict | None = None,
) -> Path:
    cap = root / run_id
    cap.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "status": status,
        "trace_ref": "trace.jsonl",
        "tool_calls_ref": "tool-calls.jsonl",
        "model_calls_ref": "model-calls.jsonl",
    }
    if command is not None:
        manifest["command"] = command
    if error is not None:
        manifest["error"] = error
    (cap / "capsule.yaml").write_text(yaml.dump(manifest))
    (cap / "trace.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in (trace or []))
    )
    (cap / "tool-calls.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in (tool_calls or []))
    )
    (cap / "model-calls.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in (model_calls or []))
    )
    return cap


def _script(tmp_path: Path, body: str) -> list[str]:
    path = tmp_path / "agent.py"
    path.write_text(body)
    return [sys.executable, str(path)]


class TestSynthesize:
    def test_model_hypothesis_maps_to_mutate_payload(self, tmp_path: Path) -> None:
        cap = _write_capsule(
            tmp_path, "run-syn", model_calls=_erroring_model_calls()
        )
        attribution = attribute_failure(cap)
        assert attribution.responsible is not None
        payload = synthesize_intervention_payload(
            attribution.responsible, _erroring_model_calls()
        )
        assert payload["target"] == {"stream": "model-calls", "event_index": 1}
        assert payload["mutate_payload"]["status"] == "ok"
        assert payload["mutate_payload"]["error"] is None

    def test_non_model_hypothesis_is_unmappable(self, tmp_path: Path) -> None:
        cap = _write_capsule(
            tmp_path,
            "run-tool",
            tool_calls=[
                {
                    "span_id": "t1",
                    "tool_name": "http_post",
                    "status": "error",
                    "error": "request failed",
                }
            ],
        )
        attribution = attribute_failure(cap)
        assert attribution.responsible is not None
        with pytest.raises(UnmappableHypothesisError, match="hypothesis class"):
            synthesize_intervention_payload(attribution.responsible, [])

    def test_unlocatable_model_step_is_unmappable(self, tmp_path: Path) -> None:
        # Model record without any id key -> synthetic step_id, never locatable.
        calls = [{"model": "m", "status": "error", "error": "boom"}]
        cap = _write_capsule(tmp_path, "run-noid", model_calls=calls)
        attribution = attribute_failure(cap)
        assert attribution.responsible is not None
        with pytest.raises(UnmappableHypothesisError, match="not found"):
            synthesize_intervention_payload(attribution.responsible, calls)


class TestVerifyHypothesis:
    def test_confirmed_when_intervention_flips_outcome(self, tmp_path: Path) -> None:
        cap = _write_capsule(
            tmp_path,
            "run-confirm",
            command=_script(tmp_path, _CHECK_QUEUE_SCRIPT),
            model_calls=_erroring_model_calls(),
            error={"type": "ModelError", "message": "model call rejected"},
        )
        attribution = attribute_failure(cap)
        v = verify_hypothesis(
            cap, attribution, replays_base_dir=tmp_path / "replays"
        )
        assert v.verdict is Verdict.CONFIRMED
        assert v.hypothesis is not None
        assert v.hypothesis["step_id"] == "m1"
        assert v.intervention is not None
        assert v.intervention["substitution"] == "mutate_payload"
        assert v.original_outcome == {"status": "failure"}
        assert v.counterfactual_outcome is not None
        assert v.counterfactual_outcome["status"] == "success"
        assert v.counterfactual_outcome["exit_code"] == 0
        # the intervened capsule is written, hard-marked, and referenced
        assert v.intervened_capsule is not None
        manifest = yaml.safe_load(
            (Path(v.intervened_capsule) / "capsule.yaml").read_text()
        )
        assert manifest["replay_mode"] == "intervention"
        assert manifest["replay_of_run_id"] == "run-confirm"

    def test_refuted_when_outcome_does_not_flip(self, tmp_path: Path) -> None:
        cap = _write_capsule(
            tmp_path,
            "run-refute",
            command=_script(tmp_path, _ALWAYS_FAIL_SCRIPT),
            model_calls=_erroring_model_calls(),
        )
        attribution = attribute_failure(cap)
        v = verify_hypothesis(
            cap, attribution, replays_base_dir=tmp_path / "replays"
        )
        assert v.verdict is Verdict.REFUTED
        assert v.counterfactual_outcome is not None
        assert v.counterfactual_outcome["status"] == "failure"
        assert "did not flip" in v.reason

    def test_inconclusive_for_unmappable_hypothesis_class(
        self, tmp_path: Path
    ) -> None:
        cap = _write_capsule(
            tmp_path,
            "run-toolcls",
            command=_script(tmp_path, _CHECK_QUEUE_SCRIPT),
            tool_calls=[
                {
                    "span_id": "t1",
                    "tool_name": "http_post",
                    "status": "error",
                    "error": "request failed",
                }
            ],
        )
        attribution = attribute_failure(cap)
        v = verify_hypothesis(
            cap, attribution, replays_base_dir=tmp_path / "replays"
        )
        assert v.verdict is Verdict.INCONCLUSIVE
        assert "cannot auto-intervene" in v.reason
        assert v.intervention is None
        assert v.counterfactual_outcome is None

    def test_inconclusive_without_reexecutable_command(self, tmp_path: Path) -> None:
        cap = _write_capsule(
            tmp_path, "run-nocmd", model_calls=_erroring_model_calls()
        )
        attribution = attribute_failure(cap)
        v = verify_hypothesis(
            cap, attribution, replays_base_dir=tmp_path / "replays"
        )
        assert v.verdict is Verdict.INCONCLUSIVE
        assert "no re-executable command" in v.reason
        # the intervention ran but the flip is not measurable — recorded, not guessed
        assert v.intervention is not None
        assert v.counterfactual_outcome is not None
        assert v.counterfactual_outcome["exit_code"] is None

    def test_inconclusive_without_hypothesis(self, tmp_path: Path) -> None:
        cap = _write_capsule(tmp_path, "run-nohyp", status="failure")
        attribution = attribute_failure(cap)
        assert attribution.responsible is None
        v = verify_hypothesis(
            cap, attribution, replays_base_dir=tmp_path / "replays"
        )
        assert v.verdict is Verdict.INCONCLUSIVE
        assert "no failure hypothesis" in v.reason
        assert v.hypothesis is None

    def test_inconclusive_when_original_run_did_not_fail(
        self, tmp_path: Path
    ) -> None:
        cap = _write_capsule(
            tmp_path,
            "run-ok",
            status="success",
            model_calls=_erroring_model_calls(),
        )
        attribution = attribute_failure(cap)
        v = verify_hypothesis(
            cap, attribution, replays_base_dir=tmp_path / "replays"
        )
        assert v.verdict is Verdict.INCONCLUSIVE
        assert "not a failure" in v.reason

    def test_inconclusive_when_replay_aborts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from novafabric.replay import _engine as engine_mod
        from novafabric.replay._result import ReplayResult

        def _aborted(self: object) -> ReplayResult:
            return ReplayResult(
                replay_id="RID",
                replay_of_run_id="run-abort",
                mode="intervention",
                status="aborted",
                start_time="t0",
                end_time="t1",
                duration_ms=1,
                policy_flags_used=[],
                env_warnings=[],
                error={"type": "FatalCheckFailed", "message": "fatal check failed"},
            )

        monkeypatch.setattr(engine_mod.ReplayEngine, "run", _aborted)
        cap = _write_capsule(
            tmp_path, "run-abort", model_calls=_erroring_model_calls()
        )
        attribution = attribute_failure(cap)
        v = verify_hypothesis(
            cap, attribution, replays_base_dir=tmp_path / "replays"
        )
        assert v.verdict is Verdict.INCONCLUSIVE
        assert "aborted" in v.reason

    def test_inconclusive_on_named_intervention_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from novafabric.replay import _engine as engine_mod
        from novafabric.replay._intervention import InterventionTargetNotFoundError

        def _boom(self: object) -> None:
            raise InterventionTargetNotFoundError("no such event")

        monkeypatch.setattr(engine_mod.ReplayEngine, "run", _boom)
        cap = _write_capsule(
            tmp_path, "run-err", model_calls=_erroring_model_calls()
        )
        attribution = attribute_failure(cap)
        v = verify_hypothesis(
            cap, attribution, replays_base_dir=tmp_path / "replays"
        )
        assert v.verdict is Verdict.INCONCLUSIVE
        assert "intervention replay failed" in v.reason


class TestCli:
    def _confirmable_capsule(self, tmp_path: Path, run_id: str) -> Path:
        return _write_capsule(
            tmp_path,
            run_id,
            command=_script(tmp_path, _CHECK_QUEUE_SCRIPT),
            model_calls=_erroring_model_calls(),
        )

    def test_json_output_includes_verification_block(self, tmp_path: Path) -> None:
        from novafabric.cli.main import app

        self._confirmable_capsule(tmp_path, "run-cli-json")
        result = runner.invoke(
            app,
            [
                "diagnose",
                "run-cli-json",
                "--capsule-dir",
                str(tmp_path),
                "--intervene",
                "--replay-dir",
                str(tmp_path / "replays"),
                "--output",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        verification = payload["verification"]
        assert verification["verdict"] == "CONFIRMED"
        assert verification["hypothesis"]["step_id"] == "m1"
        assert verification["intervention"]["substitution"] == "mutate_payload"
        assert verification["original_outcome"] == {"status": "failure"}
        assert verification["counterfactual_outcome"]["status"] == "success"
        assert verification["intervened_capsule"]
        assert "mocked semantics" in verification["note"]

    def test_text_output_prints_verdict_block(self, tmp_path: Path) -> None:
        from novafabric.cli.main import app

        self._confirmable_capsule(tmp_path, "run-cli-text")
        result = runner.invoke(
            app,
            [
                "diagnose",
                "run-cli-text",
                "--capsule-dir",
                str(tmp_path),
                "--intervene",
                "--replay-dir",
                str(tmp_path / "replays"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Hypothesis verification" in result.output
        assert "CONFIRMED" in result.output

    def test_text_verdict_block_without_hypothesis(self, tmp_path: Path) -> None:
        from novafabric.cli.main import app

        _write_capsule(tmp_path, "run-cli-nohyp", status="failure")
        result = runner.invoke(
            app,
            [
                "diagnose",
                "run-cli-nohyp",
                "--capsule-dir",
                str(tmp_path),
                "--intervene",
                "--replay-dir",
                str(tmp_path / "replays"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "INCONCLUSIVE" in result.output
        assert "none applied" in result.output

    def test_default_output_unchanged_without_flag(self, tmp_path: Path) -> None:
        from novafabric.cli.main import app

        self._confirmable_capsule(tmp_path, "run-cli-plain")
        result = runner.invoke(
            app,
            [
                "diagnose",
                "run-cli-plain",
                "--capsule-dir",
                str(tmp_path),
                "--output",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert "verification" not in payload

    def test_replay_dir_requires_intervene(self, tmp_path: Path) -> None:
        from novafabric.cli.main import app

        self._confirmable_capsule(tmp_path, "run-cli-guard")
        result = runner.invoke(
            app,
            [
                "diagnose",
                "run-cli-guard",
                "--capsule-dir",
                str(tmp_path),
                "--replay-dir",
                str(tmp_path / "replays"),
            ],
        )
        assert result.exit_code == 1
        assert_flag_in_help(result, "--replay-dir")

    def test_missing_capsule_exits_nonzero(self, tmp_path: Path) -> None:
        from novafabric.cli.main import app

        result = runner.invoke(
            app,
            [
                "diagnose",
                "does-not-exist",
                "--capsule-dir",
                str(tmp_path),
                "--intervene",
            ],
        )
        assert result.exit_code == 1
        assert "does-not-exist" in result.output

    def test_help_shows_intervene_flag(self) -> None:
        from novafabric.cli.main import app

        result = runner.invoke(app, ["diagnose", "--help"])
        assert result.exit_code == 0
        assert_flag_in_help(result, "--intervene")
