"""ADR-0306 Q7 (Option 1): when an adapter capsule may be re-run, and how.

An ``sdk-decorator`` capsule is re-executable only if it carries the
``process_invocation`` facet **and** every guard passes. Each guard has its own
refusal reason (``guard: <name>``). A capsule without the facet -- every adapter
capsule written before this change, like RQ1's ``@langgraph:code-reviewer`` -- is
refused with the message it always got, byte for byte.
"""

from __future__ import annotations

import hashlib
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
    GUARDS,
    LONG_LIVED_HOSTS,
    not_reexecutable_reason,
    refusal_message,
    require_reexecution_plan,
)

REPO = Path(__file__).resolve().parents[2]
_RESULT_SCHEMA = json.loads(
    (REPO / "src" / "novafabric" / "schemas" / "replay-result.schema.json").read_text()
)
runner = CliRunner()

#: The exact refusal an RQ1-shaped capsule got before ADR-0306 Q7 -- and still gets.
GOLDEN_OLD_MESSAGE = (
    "--mode mocked cannot replay it: this capsule was captured inside a framework call, "
    "by a framework adapter or the novafabric.sdk.agent decorator (capture_mode: "
    "sdk-decorator) and records no command to re-run ('@langgraph:code-reviewer' is a "
    "label, not a program). Modes that work on it: forensic (inspect the capsule) and "
    "semantic (score the recorded responses); --mode exact reports it not eligible. "
    "Re-running such a capsule is not supported (ADR-0306, open question 7)."
)


def _rq1_manifest() -> dict[str, Any]:
    """The manifest shape RQ1's LangGraph adapter capsules carry (no facet)."""
    return {
        "schema_version": "0.1.0",
        "run_id": "01KTB887AAAAAAAAAAAAAAAAAA",
        "status": "success",
        "command": ["@langgraph:code-reviewer"],
        "capture_mode": "sdk-decorator",
        "working_directory": "~/bench",
        "exit_code": 0,
        "metadata": {"framework": "langgraph", "wire_capture": "complete"},
    }


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "agent.py").write_text("print('agent')\n")
    return proj


def _facet(project: Path, **overrides: Any) -> dict[str, Any]:
    script = project / "agent.py"
    facet: dict[str, Any] = {
        "schema_version": "0.1.0",
        "argv": ["/usr/bin/python3", "agent.py", "--snippet", "a.py"],
        "entry_kind": "script",
        "entry": "agent.py",
        "entry_file": str(script),
        "entry_sha256": _sha(script),
        "cwd": str(project),
        "stdin_isatty": False,
        "adapter_run_ordinal": 1,
        "host_modules": [],
    }
    facet.update(overrides)
    return facet


def _manifest(facet: dict[str, Any] | None) -> dict[str, Any]:
    manifest = _rq1_manifest()
    if facet is not None:
        manifest["facets"] = {"process_invocation": facet}
    return manifest


# --- an old capsule: unchanged ------------------------------------------------------


def test_a_capsule_without_the_facet_gets_the_exact_old_message() -> None:
    assert refusal_message(_rq1_manifest(), "mocked") == GOLDEN_OLD_MESSAGE
    with pytest.raises(CapsuleNotReplayableError) as exc:
        require_reexecution_plan(_rq1_manifest(), "mocked")
    assert str(exc.value) == GOLDEN_OLD_MESSAGE
    assert "guard" not in exc.value.as_error()


def test_an_old_capsule_exits_1_through_the_cli(tmp_path: Path) -> None:
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "capsule.yaml").write_text(yaml.safe_dump(_rq1_manifest()))
    out = runner.invoke(
        app, ["replay", "-o", str(tmp_path / "r"), str(cap)], env={"COLUMNS": "400"}
    )
    assert out.exit_code == 1, out.output
    (replay_dir,) = list((tmp_path / "r").iterdir())
    result = yaml.safe_load((replay_dir / "replay_result.yaml").read_text())
    assert result["error"]["message"] == GOLDEN_OLD_MESSAGE
    jsonschema.validate(result, _RESULT_SCHEMA)


# --- the guards -----------------------------------------------------------------------


def test_a_facet_passing_every_guard_yields_a_plan(project: Path) -> None:
    plan = require_reexecution_plan(_manifest(_facet(project)), "mocked")
    assert plan.command == ["/usr/bin/python3", "agent.py", "--snippet", "a.py"]
    assert plan.cwd == project
    assert plan.source == "process_invocation"
    assert plan.adapter_runs_expected == 1
    assert not_reexecutable_reason(_manifest(_facet(project))) is None


def test_a_home_relative_facet_is_expanded_on_the_replay_host(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(project.parent))
    facet = _facet(
        project,
        argv=["~/venv/bin/python", "agent.py", "~/proj/in.txt"],
        entry_file="~/proj/agent.py",
        cwd="~/proj",
    )
    plan = require_reexecution_plan(_manifest(facet), "mocked")
    assert plan.command == [
        str(project.parent / "venv/bin/python"), "agent.py", str(project / "in.txt"),
    ]
    assert plan.cwd == project


def test_code_is_re_run_and_needs_no_entry_file(project: Path) -> None:
    facet = _facet(
        project, argv=["/usr/bin/python3", "-c", "import agent"], entry_kind="code",
        entry="import agent", entry_file=None, entry_sha256=None,
    )
    assert require_reexecution_plan(_manifest(facet), "mocked").command[1:] == [
        "-c", "import agent",
    ]


def _guard_cases(project: Path) -> list[tuple[str, dict[str, Any]]]:
    return [
        ("facet_malformed", {"adapter_run_ordinal": "one"}),
        ("facet_malformed", {"argv": []}),
        ("entry_not_reproducible", {"entry_kind": "interactive", "entry": None}),
        ("entry_not_reproducible", {"entry_kind": "stdin", "entry": None}),
        ("entry_not_reproducible", {"entry_kind": "unknown", "entry": None}),
        ("long_lived_host", {"host_modules": ["ipykernel"]}),
        ("long_lived_host", {"entry_kind": "module", "entry": "ipykernel_launcher",
                             "argv": ["/usr/bin/python3", "-m", "ipykernel_launcher", "-f",
                                      "k.json"]}),
        ("long_lived_host", {"entry": "/venv/bin/uvicorn", "argv": ["/usr/bin/python3",
                                                                    "/venv/bin/uvicorn"]}),
        ("long_lived_host", {"host_modules": ["_pytest"]}),
        ("argv_redacted", {"argv": ["/usr/bin/python3", "agent.py",
                                    "--key=[REDACTED:openai_api_key]"]}),
        ("not_first_adapter_run", {"adapter_run_ordinal": 2}),
        ("entry_file_missing", {"entry_file": str(project / "gone.py")}),
        ("entry_changed", {"entry_sha256": "sha256:" + "0" * 64}),
        ("entry_not_verifiable", {"entry_sha256": None}),
    ]


@pytest.mark.parametrize("case", range(14))
def test_every_guard_refuses_with_its_named_reason(project: Path, case: int) -> None:
    guard, overrides = _guard_cases(project)[case]
    manifest = _manifest(_facet(project, **overrides))
    with pytest.raises(CapsuleNotReplayableError) as exc:
        require_reexecution_plan(manifest, "mocked")
    assert exc.value.guard == guard
    error = exc.value.as_error()
    assert error["type"] == "CapsuleNotReplayable"
    assert error["guard"] == guard
    assert f"(guard: {guard})" in str(exc.value)
    # Still names the label, and never claims the capsule records no command.
    assert "'@langgraph:code-reviewer'" in str(exc.value)
    assert "records no command" not in str(exc.value)
    assert not_reexecutable_reason(manifest) is not None


def test_the_guard_cases_cover_every_guard(project: Path) -> None:
    assert {g for g, _ in _guard_cases(project)} == set(GUARDS)
    assert len(_guard_cases(project)) == 14


def test_the_long_lived_host_list_names_the_draft_hosts() -> None:
    assert {"ipykernel_launcher", "jupyter", "uvicorn", "gunicorn", "pytest",
            "streamlit"} <= LONG_LIVED_HOSTS


def test_an_entry_file_changed_after_capture_is_refused(project: Path) -> None:
    manifest = _manifest(_facet(project))
    (project / "agent.py").write_text("print('edited')\n")
    with pytest.raises(CapsuleNotReplayableError) as exc:
        require_reexecution_plan(manifest, "mocked")
    assert exc.value.guard == "entry_changed"


def test_the_facet_is_ignored_on_a_cli_wrapper_capsule(project: Path) -> None:
    manifest = {
        "capture_mode": "cli-wrapper", "command": ["python", "run.py"],
        "facets": {"process_invocation": _facet(project)},
    }
    plan = require_reexecution_plan(manifest, "mocked")
    assert plan.command == ["python", "run.py"]
    assert plan.cwd is None and plan.source == "command"


def test_otel_import_capsules_stay_refused_even_with_a_facet(project: Path) -> None:
    manifest = {"capture_mode": "otel-import", "command": [],
                "facets": {"process_invocation": _facet(project)}}
    assert not_reexecutable_reason(manifest) is not None


# --- the engine -----------------------------------------------------------------------


def _capsule(tmp_path: Path, facet: dict[str, Any] | None) -> Path:
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "capsule.yaml").write_text(yaml.safe_dump(_manifest(facet)))
    return cap


def test_mocked_runs_the_recorded_invocation_in_the_recorded_cwd(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []

    def fake_run(command: list[str], **kwargs: Any) -> Any:
        seen.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cap = _capsule(tmp_path, _facet(project))
    result = ReplayEngine(cap, ReplayFlags(mode="mocked"), base_dir=tmp_path / "r").run()
    (call,) = seen
    assert call["command"] == ["/usr/bin/python3", "agent.py", "--snippet", "a.py"]
    assert Path(call["cwd"]) == project
    assert call["stdin"] == subprocess.DEVNULL
    assert call["env"]["NOVAFABRIC_REPLAY_ADAPTER_RUNS"] == "1"
    assert call["env"]["NOVAFABRIC_REPLAY_MODE"] == "mocked"
    jsonschema.validate(result.as_dict(), _RESULT_SCHEMA)
    # The fake process made no adapter run: the replay says so, fail-closed.
    kinds = [d["kind"] for d in result.replay_contract["divergences"]]
    assert "adapter_run_count_mismatch" in kinds
    assert result.status == "failure"


def test_a_cli_wrapper_replay_keeps_its_old_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []

    def fake_run(command: list[str], **kwargs: Any) -> Any:
        seen.append(kwargs)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("NOVAFABRIC_REPLAY_ADAPTER_RUNS", "7")  # stale, inherited
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "capsule.yaml").write_text(
        yaml.safe_dump({"run_id": "r", "capture_mode": "cli-wrapper", "command": ["true"]})
    )
    ReplayEngine(cap, ReplayFlags(mode="mocked"), base_dir=tmp_path / "r").run()
    (kwargs,) = seen
    assert "cwd" not in kwargs and "stdin" not in kwargs  # owner decision O4: unchanged
    assert "NOVAFABRIC_REPLAY_ADAPTER_RUNS" not in kwargs["env"]


def test_a_guard_refusal_is_recorded_and_nothing_is_spawned(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("spawned"))
    cap = _capsule(tmp_path, _facet(project, host_modules=["ipykernel"]))
    result = ReplayEngine(cap, ReplayFlags(mode="mocked"), base_dir=tmp_path / "r").run()
    assert result.status == "aborted"
    assert result.error is not None
    assert result.error["type"] == "CapsuleNotReplayable"
    assert result.error["guard"] == "long_lived_host"
    jsonschema.validate(result.as_dict(), _RESULT_SCHEMA)


def test_dry_run_prints_the_invocation_it_would_run(tmp_path: Path, project: Path) -> None:
    cap = _capsule(tmp_path, _facet(project))
    out = runner.invoke(
        app, ["replay", "--dry-run", "-o", str(tmp_path / "r"), str(cap)],
        env={"COLUMNS": "400"},
    )
    assert out.exit_code == 0, out.output
    (replay_dir,) = list((tmp_path / "r").iterdir())
    report = (replay_dir / "dry_run_report.txt").read_text()
    assert "recorded process invocation" in report
    assert "/usr/bin/python3 agent.py --snippet a.py" in report
    assert str(project) in report


def test_exact_no_longer_says_no_command_for_a_replayable_adapter_capsule(
    tmp_path: Path, project: Path
) -> None:
    cap = _capsule(tmp_path, _facet(project))
    result = ReplayEngine(cap, ReplayFlags(mode="exact"), base_dir=tmp_path / "r").run()
    assert not any("records no command" in r for r in result.exact_reasons or [])
