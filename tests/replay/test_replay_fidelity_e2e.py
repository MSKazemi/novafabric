"""Issue #53 / ADR-0321: the outcome-equivalence (``fidelity``) verdict of a mocked
replay, and the replay capsule ``nova diff`` compares with the original.

``nova capture`` runs a real agent process (the real ``openai`` SDK over an
``httpx.MockTransport`` that answers only during capture). ``nova replay`` re-runs
it with the network refused. The verdict compares the replay with the run it
replays -- exit code, stdout and stderr digests, model/tool call sequence, the
divergence state, live calls -- and never reads ``faithful`` by default.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from _mocked_replay_agent import openai_body
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.diff._engine import DiffEngine

pytest.importorskip("openai")

runner = CliRunner()

AGENT = r'''
import json, os, sys
import httpx, openai

CANNED = json.loads(os.environ.get("FID_CANNED") or "null")


def handler(request):
    if CANNED is None:
        raise RuntimeError("network reached during replay")
    return httpx.Response(200, json=CANNED)


client = openai.OpenAI(
    api_key="test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler))
)
resp = client.chat.completions.create(
    model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
)
print("answer:", resp.choices[0].message.content)
extra = os.environ.get("FID_EXTRA")
if extra:
    print(extra)
counter = os.environ.get("FID_COUNTER")
if counter:
    # A file side effect that a replay repeats (probe P6), made visible on stdout.
    with open(counter, "a") as fh:
        fh.write("x\n")
    with open(counter) as fh:
        print("lines:", len(fh.read().splitlines()))
print("done", file=sys.stderr)
sys.exit(int(os.environ.get("FID_EXIT", "0")))
'''


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    agent = tmp_path / "agent.py"
    agent.write_text(AGENT)
    for var in ("FORCE_COLOR", "COLORTERM", "FID_CANNED", "FID_EXTRA", "FID_COUNTER", "FID_EXIT"):
        monkeypatch.delenv(var, raising=False)
    return {"agent": agent, "runs": tmp_path / "runs", "replays": tmp_path / "replays"}


def _capture(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("FID_CANNED", json.dumps(openai_body(content="Paris", rid="r1")))
    result = runner.invoke(
        app, ["capture", "--output-dir", str(env["runs"]), sys.executable, str(env["agent"])]
    )
    monkeypatch.delenv("FID_CANNED")
    capsules = [p for p in env["runs"].iterdir() if (p / "capsule.yaml").exists()]
    assert len(capsules) == 1, result.output
    return capsules[0]


def _replay(
    env: dict[str, Path], capsule: Path, *args: str
) -> tuple[Any, dict[str, Any], Path]:
    before = set(env["replays"].iterdir()) if env["replays"].exists() else set()
    result = runner.invoke(app, ["replay", str(capsule), "-o", str(env["replays"]), *args])
    (replay_dir,) = set(env["replays"].iterdir()) - before
    replay = yaml.safe_load((replay_dir / "replay_result.yaml").read_text())
    return result, replay, replay_dir


def _failing(replay: dict[str, Any]) -> list[str]:
    return [c["name"] for c in replay["fidelity"]["checks"] if c["equal"] is False]


def _check(replay: dict[str, Any], name: str) -> dict[str, Any]:
    (check,) = [c for c in replay["fidelity"]["checks"] if c["name"] == name]
    return check


def test_a_faithful_replay_is_faithful_and_its_capsule_diffs_clean(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule = _capture(env, monkeypatch)
    result, replay, replay_dir = _replay(env, capsule)

    assert result.exit_code == 0, (result.output, replay)
    assert replay["status"] == "success"
    fidelity = replay["fidelity"]
    assert fidelity["verdict"] == "faithful", fidelity
    assert fidelity["normalization"] == "none"
    names = [c["name"] for c in fidelity["checks"]]
    for name in ("exit_code", "stdout_sha256", "stderr_sha256", "model_call_sequence",
                 "tool_call_sequence", "divergence", "live_calls"):
        assert name in names
    assert all(c["equal"] is True for c in fidelity["checks"] if c["name"] != "tool_result_echo")
    assert _check(replay, "exit_code")["recorded"] == 0
    assert _check(replay, "stdout_sha256")["recorded"] == _check(replay, "stdout_sha256")["replayed"]
    assert "fidelity: faithful" in result.output

    # The replay capsule: hard-marked, and `nova diff` finds no behavioural change.
    manifest = yaml.safe_load((replay_dir / "capsule.yaml").read_text())
    assert manifest["replay_mode"] == "mocked"
    assert manifest["replay_of_run_id"] == yaml.safe_load(
        (capsule / "capsule.yaml").read_text()
    )["run_id"]
    assert manifest["exit_code"] == 0
    assert replay["replay_capsule_ref"] == "capsule.yaml"
    assert (replay_dir / "outputs" / "stdout.txt").read_bytes() == (
        capsule / "outputs" / "stdout.txt"
    ).read_bytes()
    report = DiffEngine().compare(capsule, replay_dir)
    assert not report.has_changes, report.as_dict()
    diff = runner.invoke(app, ["diff", "--assert-no-regressions", str(capsule), str(replay_dir)])
    assert diff.exit_code == 0, diff.output

    gate = runner.invoke(
        app, ["replay", str(capsule), "-o", str(env["replays"]), "--require-faithful"]
    )
    assert gate.exit_code == 0, gate.output


def test_changed_stdout_is_diverged_and_names_the_stdout_check(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule = _capture(env, monkeypatch)
    monkeypatch.setenv("FID_EXTRA", "something else happened")
    result, replay, replay_dir = _replay(env, capsule)

    # The process outcome is unchanged: status keeps its meaning.
    assert result.exit_code == 0, (result.output, replay)
    assert replay["status"] == "success"
    assert replay["fidelity"]["verdict"] == "diverged"
    assert _failing(replay) == ["stdout_sha256"]
    assert "fidelity: diverged" in result.output
    assert "stdout_sha256" in result.output

    report = DiffEngine().compare(capsule, replay_dir)
    assert report.has_changes
    assert [c["path"] for c in report.output_changes] == ["outputs/stdout.txt"]
    diff = runner.invoke(app, ["diff", "--assert-no-regressions", str(capsule), str(replay_dir)])
    assert diff.exit_code == 1, diff.output

    gate = runner.invoke(
        app, ["replay", str(capsule), "-o", str(env["replays"]), "--require-faithful"]
    )
    assert gate.exit_code == 4, gate.output
    assert "--require-faithful" in gate.output


def test_a_repeated_file_side_effect_that_shows_on_stdout_is_diverged(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Probe P6: the replay appends to the same file again. Mocked replay does not
    intercept file writes; the verdict sees it only because the workload prints."""
    monkeypatch.setenv("FID_COUNTER", str(tmp_path / "counter.txt"))
    capsule = _capture(env, monkeypatch)
    _result, replay, _dir = _replay(env, capsule)

    assert replay["status"] == "success"
    assert replay["fidelity"]["verdict"] == "diverged"
    assert _failing(replay) == ["stdout_sha256"]


def test_a_faithful_replay_of_a_failed_run_is_failure_and_faithful(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FID_EXIT", "1")
    capsule = _capture(env, monkeypatch)
    assert yaml.safe_load((capsule / "capsule.yaml").read_text())["exit_code"] == 1
    result, replay, replay_dir = _replay(env, capsule)

    assert result.exit_code == 1
    assert replay["status"] == "failure"
    assert replay["exit_code"] == 1
    assert replay["fidelity"]["verdict"] == "faithful", replay["fidelity"]
    exit_check = _check(replay, "exit_code")
    assert (exit_check["recorded"], exit_check["replayed"], exit_check["equal"]) == (1, 1, True)
    assert not DiffEngine().compare(capsule, replay_dir).has_changes

    # --require-faithful gates on the verdict, not on the process outcome.
    gate = runner.invoke(
        app, ["replay", str(capsule), "-o", str(env["replays"]), "--require-faithful"]
    )
    assert gate.exit_code == 0, gate.output


def test_a_changed_exit_code_is_diverged(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule = _capture(env, monkeypatch)
    monkeypatch.setenv("FID_EXIT", "3")
    result, replay, _dir = _replay(env, capsule)

    assert result.exit_code == 3
    assert replay["status"] == "failure"
    assert replay["fidelity"]["verdict"] == "diverged"
    assert _failing(replay) == ["exit_code"]


def _strip_outputs_record(capsule: Path) -> None:
    """A capsule from before ADR-0251's evidence digests, with no stored output:
    an absent stdout.txt cannot be told apart from an unrecorded one."""
    import shutil

    shutil.rmtree(capsule / "outputs")
    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    manifest.pop("evidence_digests", None)
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(manifest))


def test_a_capsule_without_recorded_outputs_is_not_compared(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule = _capture(env, monkeypatch)
    _strip_outputs_record(capsule)
    result, replay, _dir = _replay(env, capsule)

    assert result.exit_code == 0, (result.output, replay)
    assert replay["status"] == "success"
    assert replay["fidelity"]["verdict"] == "not_compared", replay["fidelity"]
    assert _check(replay, "stdout_sha256")["equal"] is None
    assert _check(replay, "stderr_sha256")["equal"] is None
    assert _check(replay, "stdout_sha256")["note"]
    assert "not compared" in result.output

    gate = runner.invoke(
        app, ["replay", str(capsule), "-o", str(env["replays"]), "--require-faithful"]
    )
    assert gate.exit_code == 4, gate.output


def test_a_capsule_that_recorded_no_exit_code_is_not_compared(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule = _capture(env, monkeypatch)
    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    del manifest["exit_code"]
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    _result, replay, _dir = _replay(env, capsule)

    assert replay["fidelity"]["verdict"] == "not_compared"
    assert _check(replay, "exit_code")["equal"] is None


def test_require_faithful_only_applies_to_mocked_mode(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule = _capture(env, monkeypatch)
    result = runner.invoke(
        app, ["replay", str(capsule), "--mode", "forensic", "--require-faithful",
              "-o", str(env["replays"])]
    )
    assert result.exit_code == 1
    assert "--require-faithful only applies to --mode mocked" in result.output
