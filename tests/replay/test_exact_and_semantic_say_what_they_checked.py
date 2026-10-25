"""``--mode exact`` and ``--mode semantic`` do not report a result they did not earn
(review replay-lineage-registry-09).

Exact reported ``status: success`` (a green tick, exit 0) for a capsule that was
not eligible for exact replay, and the CLI never printed why. Semantic reported
``similarity_score: 1.0`` and ``matched_run_id: <same run>`` when fewer than two
responses existed: a perfect score built from no data.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.replay._engine import ReplayEngine
from novafabric.replay._flags import ReplayFlags

runner = CliRunner()


def _capsule(tmp_path: Path, *, env_mode: str, calls: list[dict[str, Any]]) -> Path:
    cap = tmp_path / "capsules" / "RUN01"
    cap.mkdir(parents=True)
    (cap / "inputs").mkdir()
    (cap / "outputs").mkdir()
    (cap / "capsule.yaml").write_text(yaml.dump({
        "schema_version": "0.1.0", "run_id": "RUN01", "status": "success",
        "command": ["python", "-c", "pass"], "capture_mode": "cli-wrapper",
        "novafabric_version": "0.2.0", "model_call_count": len(calls), "tool_call_count": 0,
    }))
    (cap / "env.lock").write_text(yaml.dump({
        "schema_version": "0.1.0", "mode": env_mode,
        "python": {"version": "3.12.3", "interpreter": "cpython"},
        "host": {"os": "linux", "arch": "x86_64"},
    }))
    (cap / "replay.yaml").write_text(yaml.dump({"schema_version": "0.1.0"}))
    (cap / "model-calls.jsonl").write_text("".join(json.dumps(c) + "\n" for c in calls))
    for name in ("trace.jsonl", "assets.jsonl", "tool-calls.jsonl"):
        (cap / name).write_text("")
    (cap / "redaction-proof.json").write_text(json.dumps({"findings": []}))
    return cap


def _call(content: str | None, *, seed: int | None = 7) -> dict[str, Any]:
    record: dict[str, Any] = {
        "model_call_id": "01CALLCALLCALL", "gen_ai.request.model": "m",
        "gen_ai.response.choices": (
            [{"message": {"role": "assistant", "content": content}}] if content else []
        ),
    }
    if seed is not None:
        record["gen_ai.request.seed"] = seed
    return record


def _run(cap: Path, mode: str, tmp_path: Path) -> Any:
    return ReplayEngine(
        capsule_dir=cap, flags=ReplayFlags(mode=mode), base_dir=tmp_path / "replays"
    ).run()


def test_an_ineligible_capsule_is_not_a_successful_exact_replay(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, env_mode="best-effort", calls=[_call("hi")])
    result = _run(cap, "exact", tmp_path)
    assert result.exact_eligible is False
    assert result.status == "failure"
    assert result.exit_code == 1
    assert "deterministic" in (result.divergence_reason or "")


def test_an_eligible_capsule_is_still_a_success(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, env_mode="deterministic", calls=[_call("hi")])
    result = _run(cap, "exact", tmp_path)
    assert result.exact_eligible is True and result.status == "success"
    assert result.divergence_reason is None


def test_the_cli_prints_why_and_exits_nonzero(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, env_mode="best-effort", calls=[_call("hi")])
    result = runner.invoke(
        app, ["replay", str(cap), "--mode", "exact", "--output-dir", str(tmp_path / "out")]
    )
    assert result.exit_code == 1, result.output
    assert "not eligible for exact replay" in result.output
    assert "deterministic" in result.output
    assert "✓" not in result.output


def test_the_cli_says_when_a_capsule_is_eligible(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, env_mode="deterministic", calls=[_call("hi")])
    result = runner.invoke(
        app, ["replay", str(cap), "--mode", "exact", "--output-dir", str(tmp_path / "out")]
    )
    assert result.exit_code == 0, result.output
    assert "eligible for exact replay" in result.output


def test_semantic_with_nothing_to_compare_has_no_score(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, env_mode="best-effort", calls=[_call(None)])
    result = _run(cap, "semantic", tmp_path)
    assert result.similarity_score is None
    assert result.matched_run_id is None
    assert "fewer than 2" in (result.divergence_reason or "")


def test_semantic_with_two_responses_scores_them(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, env_mode="best-effort", calls=[_call("same"), _call("same")])
    result = _run(cap, "semantic", tmp_path)
    assert result.similarity_score == 1.0
    assert result.matched_run_id == "RUN01"
    assert result.divergence_reason is None


def test_the_cli_prints_the_semantic_score_or_why_there_is_none(tmp_path: Path) -> None:
    scored = _capsule(tmp_path, env_mode="best-effort", calls=[_call("a b"), _call("a c")])
    out = runner.invoke(
        app, ["replay", str(scored), "--mode", "semantic", "--output-dir", str(tmp_path / "o1")]
    )
    assert out.exit_code == 0 and "similarity" in out.output, out.output
