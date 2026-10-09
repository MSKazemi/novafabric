"""A capsule that records no command is refused up front, never spawned.

Found 2026-10-09: ``nova replay --mode mocked`` of every framework-adapter
capsule failed with ``the replayed command could not be run: [Errno 2] No such
file or directory: '@langgraph:demo'``. Adapter capsules (and the
``@novafabric.agent`` decorator's) are captured *inside* a framework call; their
``command`` is a ``@framework:name`` label with ``capture_mode: sdk-decorator``,
and the engine handed that label to ``subprocess.run`` as if it were a program.

Now one helper (``replay/_replayability.py``) decides, before anything is
spawned: ``mocked`` aborts with ``error.type: CapsuleNotReplayable`` and a
message naming the modes that do work; ``--dry-run`` says the same and exits 1;
``intervention`` emits the counterfactual streams without re-running and says
so; ``exact`` reports the capsule ineligible; ``forensic`` and ``semantic`` are
unchanged.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.replay import ReplayEngine, ReplayFlags
from novafabric.replay._errors import CapsuleNotReplayableError
from novafabric.replay._replayability import (
    MODES_THAT_REEXECUTE,
    MODES_WITHOUT_REEXECUTION,
    NON_REEXECUTABLE_CAPTURE_MODES,
    not_reexecutable_reason,
    require_reexecutable_command,
)

_REPO = Path(__file__).resolve().parents[2]
_SCHEMA = json.loads(
    (_REPO / "src" / "novafabric" / "schemas" / "replay-result.schema.json").read_text()
)

runner = CliRunner()


@pytest.fixture
def adapter_capsule(tmp_path: Path) -> Path:
    """A real capsule written by the shared framework-adapter core."""
    from novafabric.adapters._capsule import begin_capture

    cap = begin_capture(framework="langgraph", run_name="demo", data_dir=tmp_path / "caps")
    cap.finish()
    manifest = yaml.safe_load((cap.cap_dir / "capsule.yaml").read_text())
    assert manifest["command"] == ["@langgraph:demo"]
    assert manifest["capture_mode"] == "sdk-decorator"
    return cap.cap_dir


@pytest.fixture
def no_spawn(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Fail the test if the engine tries to launch anything."""
    calls: list[Any] = []

    def refuse(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        raise AssertionError(f"replay spawned a process: {args!r}")

    monkeypatch.setattr(subprocess, "run", refuse)
    return calls


def _run(capsule: Path, tmp_path: Path, **flags: Any) -> Any:
    return ReplayEngine(
        capsule_dir=capsule, flags=ReplayFlags(**flags), base_dir=tmp_path / "replays"
    ).run()


# --- the helper ---------------------------------------------------------------


@pytest.mark.parametrize(
    "manifest",
    [
        {"capture_mode": "sdk-decorator", "command": ["@langgraph:demo"]},
        {"capture_mode": "sdk-decorator", "command": ["@agent:triage@1.0"]},
        {"capture_mode": "otel-import", "command": []},
        {"capture_mode": "cli-wrapper", "command": []},
        {"command": ["@crewai:run"]},
        {"run_id": "no-command-key"},
    ],
)
def test_non_reexecutable_capsules_are_recognised(manifest: dict[str, Any]) -> None:
    assert not_reexecutable_reason(manifest) is not None
    with pytest.raises(CapsuleNotReplayableError) as exc:
        require_reexecutable_command(manifest, "mocked")
    message = str(exc.value)
    assert "records no command to re-run" in message
    assert "forensic" in message and "semantic" in message
    assert exc.value.as_error()["type"] == "CapsuleNotReplayable"


def test_a_real_command_is_returned_unchanged() -> None:
    manifest = {"capture_mode": "cli-wrapper", "command": ["python", "agent.py"]}
    assert not_reexecutable_reason(manifest) is None
    assert require_reexecutable_command(manifest, "mocked") == ["python", "agent.py"]


def test_the_message_names_the_framework_label() -> None:
    reason = not_reexecutable_reason(
        {"capture_mode": "sdk-decorator", "command": ["@langgraph:demo"]}
    )
    assert reason is not None
    assert "'@langgraph:demo' is a label, not a program" in reason
    assert "framework call" in reason


def _manifest_capture_modes() -> dict[str, str]:
    """capture_mode literal of every manifest dict in the adapters and the SDK."""
    src = _REPO / "src" / "novafabric"
    files = sorted((src / "adapters").glob("*.py")) + [src / "sdk" / "agent.py"]
    found: dict[str, str] = {}
    for path in files:
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Dict):
                continue
            pairs = {
                k.value: v
                for k, v in zip(node.keys, node.values)
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
            if {"run_id", "command", "capture_mode"} <= pairs.keys():
                mode = pairs["capture_mode"]
                assert isinstance(mode, ast.Constant), path
                found[f"{path.name}:{node.lineno}"] = mode.value
    return found


def test_every_adapter_and_sdk_writer_is_covered_by_the_helper() -> None:
    """A new in-process writer with a new capture_mode must be classified here."""
    modes = _manifest_capture_modes()
    assert len(modes) >= 10, modes  # 9 adapters + the shared core + the SDK
    unknown = {where: m for where, m in modes.items() if m not in NON_REEXECUTABLE_CAPTURE_MODES}
    assert not unknown, unknown


def test_the_mode_lists_cover_every_cli_mode() -> None:
    from novafabric.cli.replay import ReplayMode

    assert set(MODES_THAT_REEXECUTE) | set(MODES_WITHOUT_REEXECUTION) == {
        m.value for m in ReplayMode
    }


# --- the engine ---------------------------------------------------------------


def test_mocked_refuses_an_adapter_capsule_before_spawning(
    adapter_capsule: Path, tmp_path: Path, no_spawn: list[Any]
) -> None:
    result = _run(adapter_capsule, tmp_path, mode="mocked")

    assert no_spawn == []
    assert result.status == "aborted"
    assert result.exit_code is None
    assert result.error is not None
    assert result.error["type"] == "CapsuleNotReplayable"
    assert "could not be run" not in result.error["message"]
    assert "@langgraph:demo" in result.error["message"]
    jsonschema.validate(result.as_dict(), _SCHEMA)

    # The refusal is recorded on disk, not only returned.
    written = yaml.safe_load(
        (tmp_path / "replays" / result.replay_id / "replay_result.yaml").read_text()
    )
    assert written["status"] == "aborted"
    assert written["error"]["type"] == "CapsuleNotReplayable"


def test_mocked_refuses_an_sdk_agent_capsule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.sdk import agent

    @agent(name="triage", version="1.0", capsule_dir=tmp_path / "caps")
    def work() -> str:
        return "ok"

    work()
    [manifest_path] = list((tmp_path / "caps").rglob("capsule.yaml"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("spawned"))
    result = _run(manifest_path.parent, tmp_path, mode="mocked")
    assert result.status == "aborted"
    assert result.error is not None
    assert result.error["type"] == "CapsuleNotReplayable"


@pytest.mark.parametrize("mode", ["forensic", "semantic"])
def test_read_only_modes_still_work(
    adapter_capsule: Path, tmp_path: Path, no_spawn: list[Any], mode: str
) -> None:
    result = _run(adapter_capsule, tmp_path, mode=mode)
    assert result.status == "success"
    assert result.error is None


def test_exact_reports_the_capsule_ineligible(
    adapter_capsule: Path, tmp_path: Path, no_spawn: list[Any]
) -> None:
    result = _run(adapter_capsule, tmp_path, mode="exact")
    assert result.status == "success"
    assert result.exact_eligible is False
    assert any("records no command to re-run" in r for r in result.exact_reasons or [])


def test_exact_does_not_add_the_reason_for_a_real_command(tmp_path: Path) -> None:
    capsule = tmp_path / "cap"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text(
        yaml.dump({"run_id": "r", "capture_mode": "cli-wrapper", "command": ["true"]})
    )
    (capsule / "env.lock").write_text(yaml.dump({"mode": "deterministic"}))
    result = _run(capsule, tmp_path, mode="exact")
    assert result.exact_eligible is True
    assert result.exact_reasons == []


def test_intervention_emits_streams_without_running_the_label(
    tmp_path: Path, no_spawn: list[Any]
) -> None:
    capsule = tmp_path / "cap"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text(
        yaml.dump(
            {"run_id": "r", "capture_mode": "sdk-decorator", "command": ["@langgraph:demo"]}
        )
    )
    (capsule / "model-calls.jsonl").write_text(
        json.dumps({"span_id": "s0", "model": "m", "response": {"content": "APPROVE"}})
        + "\n"
    )
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        yaml.dump(
            {
                "target": {"stream": "model-calls", "event_index": 0},
                "replace_model_response": {"content": "DENY"},
            }
        )
    )
    result = _run(capsule, tmp_path, mode="intervention", intervention_file=spec)

    assert no_spawn == []
    assert result.status == "success"
    assert result.exit_code is None
    assert result.intervention is not None
    assert result.intervention["downstream_reexecuted"] is False
    assert "@langgraph:demo" in result.intervention["downstream_not_reexecuted_reason"]
    # Nothing was re-run, so nothing received the substitution — and the note says why.
    assert result.intervention["substitution_delivered_to_workload"] is False
    assert "@langgraph:demo" in result.intervention["substitution_note"]


# --- the CLI ------------------------------------------------------------------


def test_cli_mocked_exits_1_with_the_refusal(adapter_capsule: Path, tmp_path: Path) -> None:
    out = runner.invoke(
        app,
        ["replay", "--mode", "mocked", "-o", str(tmp_path / "r"), str(adapter_capsule)],
        env={"COLUMNS": "400"},
    )
    assert out.exit_code == 1, out.output
    assert "CapsuleNotReplayable" in out.output
    assert "No such file or directory" not in out.output


def test_cli_dry_run_says_the_same_and_exits_1(adapter_capsule: Path, tmp_path: Path) -> None:
    out = runner.invoke(
        app,
        ["replay", "--dry-run", "-o", str(tmp_path / "r"), str(adapter_capsule)],
        env={"COLUMNS": "400"},
    )
    assert out.exit_code == 1, out.output
    assert "REFUSED:" in out.output
    assert "records no command to re-run" in out.output


def test_cli_dry_run_in_a_read_only_mode_is_not_refused(
    adapter_capsule: Path, tmp_path: Path
) -> None:
    out = runner.invoke(
        app,
        [
            "replay", "--dry-run", "--mode", "forensic",
            "-o", str(tmp_path / "r"), str(adapter_capsule),
        ],
    )
    assert out.exit_code == 0, out.output
    assert "REFUSED" not in out.output


def test_help_documents_the_refusal_and_exit_codes() -> None:
    out = runner.invoke(app, ["replay", "--help"], env={"COLUMNS": "200"})
    assert out.exit_code == 0
    assert "CapsuleNotReplayable" in out.output
    assert "Exit codes" in out.output
