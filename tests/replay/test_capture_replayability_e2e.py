"""Replayability at capture: ``nova capture`` writes ``replayability.json`` and says, at capture time,
whether a strict mocked replay would serve the capsule.

Real ``nova capture`` runs of small Python workloads: the ``openai`` SDK and raw
``httpx`` over an ``httpx.MockTransport`` (no network), ``record.tool`` at the
``standard`` capture level, and an adapter capsule written from a ``runpy``-style
launcher. Each summary is checked against what ``nova replay`` then does.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.capture.finalize import evidence_digests
from novafabric.cli.capture import REQUIRE_REPLAYABLE_EXIT
from novafabric.cli.main import app
from novafabric.replay._replayability_summary import REPLAYABILITY_REF, read_replayability

pytest.importorskip("httpx")

runner = CliRunner()

AGENT = r'''
import json, os, sys
import httpx

STEPS = os.environ["STEPS"].split(",")
out = {}


def chat(request):
    return httpx.Response(200, json={
        "id": "c1", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "answer"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})


if "openai" in STEPS:
    import openai
    client = openai.OpenAI(api_key="sk-test-not-a-key", base_url="http://model.invalid/v1",
                           http_client=httpx.Client(transport=httpx.MockTransport(chat)))
    r = client.chat.completions.create(model="gpt-test",
                                       messages=[{"role": "user", "content": "hi"}])
    out["openai"] = r.choices[0].message.content
if "mistral" in STEPS:
    # Raw HTTP to a registry host (url_registry: mistral). Served by the generic wire layer
    # (ADR-0324) only when its response was recorded; these tests capture with
    # NOVAFABRIC_CAPTURE_WIRE_RESPONSES=off unless they say otherwise, so it is the
    # request-only, not-servable example.
    with httpx.Client(transport=httpx.MockTransport(chat)) as http:
        for i in range(3):
            http.post("https://api.mistral.ai/v1/chat/completions",
                      json={"model": "mistral-small", "messages": [{"role": "user",
                                                                    "content": str(i)}]})
    out["mistral"] = 3
if "tool" in STEPS:
    from novafabric.capture import record

    @record.tool(mutation_class="read-only")
    def add(a, b):
        return {"sum": a + b}

    out["tool"] = [add(1, 2), add(3, 4)]
if "fail" in STEPS:
    sys.exit(3)
with open(os.environ["AGENT_OUT"], "w") as fh:
    json.dump(out, fh)
'''


class Env:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.agent = root / "agent.py"
        self.agent.write_text(AGENT)
        self.runs = root / "runs"
        self.replays = root / "replays"
        self._mp = monkeypatch
        monkeypatch.setenv("AGENT_OUT", str(root / "out.json"))
        monkeypatch.setenv("COLUMNS", "400")
        for var in ("FORCE_COLOR", "COLORTERM", "NOVA_CAPTURE_LEVEL"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("NOVAFABRIC_CAPTURE_WIRE_RESPONSES", "off")

    def capture(self, steps: str, *flags: str) -> tuple[Any, Path]:
        self._mp.setenv("STEPS", steps)
        before = set(self.runs.iterdir()) if self.runs.exists() else set()
        result = runner.invoke(
            app,
            ["capture", "--output-dir", str(self.runs), *flags, sys.executable,
             str(self.agent)],
        )
        (capsule,) = [p for p in set(self.runs.iterdir()) - before
                      if (p / "capsule.yaml").exists()]
        return result, capsule

    def replay(self, capsule: Path, *flags: str) -> tuple[Any, dict[str, Any]]:
        before = set(self.replays.iterdir()) if self.replays.exists() else set()
        result = runner.invoke(app, ["replay", str(capsule), "-o", str(self.replays), *flags])
        (replay_dir,) = set(self.replays.iterdir()) - before
        return result, yaml.safe_load((replay_dir / "replay_result.yaml").read_text())


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(tmp_path, monkeypatch)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _summary(capsule: Path) -> dict[str, Any]:
    doc = read_replayability(capsule)
    assert doc is not None, f"{REPLAYABILITY_REF} was not written"
    return doc


def test_an_openai_served_run_is_full_and_says_so(env: Env) -> None:
    pytest.importorskip("openai")
    result, capsule = env.capture("openai")
    assert result.exit_code == 0, result.output
    doc = _summary(capsule)
    assert doc["replayable"] == "full"
    assert doc["strict_mocked"] == "start"
    assert doc["model_calls"]["recorded"] == 1
    assert doc["model_calls"]["servable"] == 1
    assert "replay: strict mocked replay would serve every recorded call (1 model, 0 tool)" in (
        _flat(result.output)
    )
    replayed, outcome = env.replay(capsule)
    assert replayed.exit_code == 0, replayed.output
    assert outcome["status"] == "success"


def test_the_summary_is_bound_by_the_evidence_digests(env: Env) -> None:
    pytest.importorskip("openai")
    _, capsule = env.capture("openai")
    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    assert REPLAYABILITY_REF in manifest["evidence_digests"]
    assert manifest["evidence_digests"][REPLAYABILITY_REF] == (
        evidence_digests(capsule)[REPLAYABILITY_REF]
    )


def test_a_mistral_run_says_strict_replay_would_refuse_and_it_does(env: Env) -> None:
    result, capsule = env.capture("mistral")
    assert result.exit_code == 0, result.output
    doc = _summary(capsule)
    assert doc["replayable"] == "partial"
    assert doc["strict_mocked"] == "refuse"
    assert doc["refusal"]["type"] == "ModelSurfaceNotServable"
    (reason,) = doc["reasons"]
    assert reason["code"] == "model_surface_not_servable"
    assert reason["by_system"] == {"mistral": {"count": 3, "hosts": ["api.mistral.ai"]}}
    flat = _flat(result.output)
    assert "replay: strict mocked replay would refuse -- 3 model calls not servable" in flat
    assert "mistral: 3 (api.mistral.ai)" in flat
    assert "nova replay --dry-run" in flat
    # ...and the replay agrees.
    replayed, outcome = env.replay(capsule)
    assert replayed.exit_code == 1
    assert outcome["error"]["type"] == doc["refusal"]["type"]
    assert outcome["error"]["message"] == doc["refusal"]["message"]


def test_with_wire_responses_recorded_the_mistral_run_is_servable(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default capture (ADR-0324): the responses are recorded, so the summary counts the
    three calls as servable, as replay's own classifier does."""
    monkeypatch.delenv("NOVAFABRIC_CAPTURE_WIRE_RESPONSES", raising=False)
    result, capsule = env.capture("mistral")
    assert result.exit_code == 0, result.output
    doc = _summary(capsule)
    assert doc["model_calls"]["recorded"] == 3
    assert doc["model_calls"]["servable"] == 3
    assert doc["strict_mocked"] == "start"
    assert doc["replayable"] == "full"
    replayed, dry = env.replay(capsule, "--dry-run")
    assert replayed.exit_code == 0, replayed.output


def test_record_tool_at_the_standard_level_names_the_payload_reason(env: Env) -> None:
    result, capsule = env.capture("tool")
    assert result.exit_code == 0, result.output
    doc = _summary(capsule)
    assert doc["replayable"] == "partial"
    assert doc["strict_mocked"] == "start"
    (reason,) = doc["reasons"]
    assert reason["code"] == "tool_result_not_servable"
    assert reason["cause"] == "payload_not_recorded"
    assert reason["count"] == 2
    assert reason["tools"] == ["add"]
    assert "NOVA_CAPTURE_LEVEL=forensic" in reason["detail"]
    assert "2 tool calls whose payload was not recorded (add)" in _flat(result.output)
    # A strict replay starts, then refuses the call: the divergence the summary named.
    replayed, outcome = env.replay(capsule)
    assert replayed.exit_code != 0
    kinds = {d.get("kind") for d in outcome["replay_contract"]["divergences"]}
    assert "tool_result_not_servable" in kinds


def test_forensic_level_makes_the_tool_run_fully_replayable(
    env: Env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "forensic")
    result, capsule = env.capture("tool")
    assert result.exit_code == 0, result.output
    assert _summary(capsule)["replayable"] == "full"


# ── --require-replayable ─────────────────────────────────────────────────────


def test_require_replayable_passes_a_full_capsule(env: Env) -> None:
    pytest.importorskip("openai")
    result, _ = env.capture("openai", "--require-replayable")
    assert result.exit_code == 0, result.output


def test_require_replayable_exits_4_after_writing_the_capsule(env: Env) -> None:
    result, capsule = env.capture("mistral", "--require-replayable")
    assert result.exit_code == REQUIRE_REPLAYABLE_EXIT == 4, result.output
    # The workload ran to completion and the capsule is whole.
    assert json.loads((env.root / "out.json").read_text()) == {"mistral": 3}
    assert (capsule / "capsule.yaml").is_file()
    assert _summary(capsule)["strict_mocked"] == "refuse"
    assert "--require-replayable" in _flat(result.output)


def test_require_replayable_fails_a_capsule_that_starts_but_diverges(env: Env) -> None:
    result, _ = env.capture("tool", "--require-replayable")
    assert result.exit_code == REQUIRE_REPLAYABLE_EXIT, result.output


def test_a_failing_workload_keeps_its_own_exit_code(env: Env) -> None:
    result, capsule = env.capture("mistral,fail", "--require-replayable")
    assert result.exit_code == 3, result.output
    assert _summary(capsule)["strict_mocked"] == "refuse"


def test_without_the_flag_an_unreplayable_capsule_still_exits_0(env: Env) -> None:
    result, _ = env.capture("mistral")
    assert result.exit_code == 0, result.output


# ── adapter capsules ─────────────────────────────────────────────────────────

LAUNCHER = r'''
import runpy, sys
sys.argv = ["agent.py"]   # a harness that starts the agent through runpy
runpy.run_path(sys.argv[0], run_name="__main__")
'''

ADAPTER_AGENT = r'''
import os
from pathlib import Path
from novafabric.adapters._capsule import begin_capture

cap = begin_capture(framework="langgraph", run_name="demo",
                    data_dir=Path(os.environ["CAPS"]))
cap.finish()
'''


def test_a_runpy_launched_adapter_capsule_is_not_reproducible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "agent.py").write_text(ADAPTER_AGENT)
    caps = tmp_path / "caps"
    monkeypatch.setenv("CAPS", str(caps))
    subprocess.run([sys.executable, "-c", LAUNCHER], cwd=tmp_path, check=True,
                   timeout=120)
    (capsule,) = list(caps.iterdir())
    doc = _summary(capsule)
    assert doc["replayable"] == "none"
    assert doc["refusal"]["type"] == "CapsuleNotReplayable"
    assert doc["refusal"]["guard"] == "entry_not_reproducible"
    assert doc["reexecution"]["guard"] == "entry_not_reproducible"
    # The replay refuses with the same guard.
    result = runner.invoke(app, ["replay", str(capsule), "-o", str(tmp_path / "replays")])
    assert result.exit_code == 1
    (replay_dir,) = list((tmp_path / "replays").iterdir())
    outcome = yaml.safe_load((replay_dir / "replay_result.yaml").read_text())
    assert outcome["error"]["type"] == "CapsuleNotReplayable"
    assert outcome["error"]["guard"] == "entry_not_reproducible"


def test_a_script_launched_adapter_capsule_is_reexecutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "agent.py").write_text(ADAPTER_AGENT)
    caps = tmp_path / "caps"
    monkeypatch.setenv("CAPS", str(caps))
    subprocess.run([sys.executable, "agent.py"], cwd=tmp_path, check=True, timeout=120)
    (capsule,) = list(caps.iterdir())
    doc = _summary(capsule)
    assert doc["reexecution"] == {"reexecutable": True, "source": "process_invocation",
                                  "guard": None}
    assert doc["replayable"] == "full"


# ── old capsules ─────────────────────────────────────────────────────────────


def test_an_old_capsule_without_the_file_still_replays(env: Env) -> None:
    pytest.importorskip("openai")
    _, capsule = env.capture("openai")
    (capsule / REPLAYABILITY_REF).unlink()
    assert read_replayability(capsule) is None  # absent = unknown
    replayed, outcome = env.replay(capsule)
    assert replayed.exit_code == 0, replayed.output
    assert outcome["status"] == "success"
