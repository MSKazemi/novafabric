"""ADR-0306 slice 2: ``replay.yaml`` ``tool_overrides`` are enforced inside the
replayed process, and ``--dry-run`` prints exactly what the replay does.

Owner decisions (2026-10-09, "Strict") under test:

* **Q3** -- under ``--permissive`` an unmatched MCP call runs live only with the
  ladder flag for its class; MCP calls are always ``unknown``, so that is
  ``--allow-unknown-mutation``.
* **Q4** -- a restriction from the capsule holds (``allow: false`` is never run
  live, even under ``--permissive``); a permission (``allow: true``) takes
  effect only when the operator also passes the ladder flag.
* **Q5** -- a strict replay refuses to start (``ToolOverrideUnenforceable``,
  exit 3) when an ``allow: false`` override names a tool recorded on a transport
  replay cannot intercept; ``--permissive`` starts and reports it.

Every case re-runs a real Python agent through ``ReplayEngine``: a
``record.tool`` function, a real in-memory MCP server, and an undecorated
function standing in for a non-intercepted transport. Each appends to a
side-effect log when its body runs, so "ran live" is observed on disk, never
inferred from a counter.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from _help_assert import assert_flag_in_help, strip_ansi
from _mocked_replay_agent import mcp_record
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.replay import ReplayEngine, ReplayFlags
from novafabric.replay._policy import decide_intercepted
from novafabric.replay._result import ReplayResult

pytest.importorskip("mcp")

REPO = Path(__file__).resolve().parents[2]
_RESULT_SCHEMA = json.loads(
    (REPO / "src" / "novafabric" / "schemas" / "replay-result.schema.json").read_text()
)
_TOOL_SCHEMA = json.loads(
    (REPO / "src" / "novafabric" / "schemas" / "tool-call.schema.json").read_text()
)

AGENT = '''
import asyncio, json, os
from novafabric.capture import record

SIDE = os.environ["AGENT_SIDE"]
OUT = os.environ["AGENT_OUT"]
PLAN = json.loads(os.environ["AGENT_PLAN"])


def side(text):
    with open(SIDE, "a") as fh:
        fh.write(text + "\\n")


@record.tool  # mutation_class "unknown"
def lookup(q):
    side(f"python {q}")
    return {"q": q, "live": True}


def fetch(q):  # undeclared: a transport mocked replay does not intercept
    side(f"http {q}")
    return {"q": q, "live": True}


async def steps(session):
    out = []
    for step in PLAN:
        try:
            if step["kind"] == "python":
                value = lookup(step["q"])
            elif step["kind"] == "http":
                value = fetch(step["q"])
            else:
                res = await session.call_tool("search", {"q": step["q"]})
                value = res.content[0].text
            out.append({"value": value})
        except Exception as exc:  # the workload swallows every failure
            out.append({"error": type(exc).__name__, "message": str(exc)})
    return out


async def main():
    if not any(s["kind"] == "mcp" for s in PLAN):
        return await steps(None)
    from mcp.server.fastmcp import FastMCP
    from mcp.shared.memory import create_connected_server_and_client_session

    server = FastMCP("override-fixture")

    @server.tool()
    def search(q: str) -> str:
        side(f"mcp {q}")
        return "live"

    async with create_connected_server_and_client_session(server) as session:
        return await steps(session)


out = asyncio.run(main())
with open(OUT, "w") as fh:
    json.dump(out, fh)
'''

#: The tool name each surface's recorded call carries.
TOOL_NAME = {"python": "lookup", "python_unservable": "lookup", "mcp": "search", "http": "fetch"}
#: The plan step kind a surface is driven through.
STEP_KIND = {"python": "python", "python_unservable": "python", "mcp": "mcp", "http": "http"}
#: The value the capsule recorded for each surface (what "served" returns).
RECORDED_VALUE: dict[str, Any] = {
    "python": {"q": "a", "recorded": True},
    "mcp": "recorded",
}

_TIMES = {"started_at": "2026-10-09T00:00:00.000000Z", "finished_at": "2026-10-09T00:00:01.000000Z"}


def _base_record(name: str, transport: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "0.1.0",
        "tool_call_id": "01HXAY7M5JZ8R7K4P9DPBYK2P0",
        "parent_span_id": "0123456789abcdef",
        "duration_ms": 1000,
        "tool_name": name,
        "tool_version": "unknown",
        "tool_provider": f"{transport}://fixture",
        "transport": transport,
        "mutates": True,
        "mutation_class": "unknown",
        "arguments": arguments,
        "arguments_schema_ref": None,
        "result": None,
        "result_schema_ref": None,
        "status": "success",
        "agent_call_id": None,
        **_TIMES,
    }


def record_for(surface: str, q: str = "a") -> dict[str, Any]:
    if surface == "mcp":
        return mcp_record("search", {"q": q}, "recorded")
    if surface == "http":
        rec = _base_record("fetch", "http", {"q": q})
        rec["result"] = {"value": {"q": q, "recorded": True}}
        return rec
    rec = _base_record("lookup", "python", {"q": q})
    rec["tool_provider"] = "python://__main__"
    if surface == "python_unservable":
        rec["extensions"] = {
            "io.novafabric.tool_surface": "python.function",
            "io.novafabric.result_codec": "not-servable",
            "io.novafabric.not_servable_reason": "the result is not JSON-native (tuple)",
        }
        return rec
    rec["result"] = {"value": {"q": q, "recorded": True}}
    rec["extensions"] = {
        "io.novafabric.tool_surface": "python.function",
        "io.novafabric.result_codec": "json-v1",
    }
    return rec


class Workload:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.agent = root / "agent.py"
        self.agent.write_text(AGENT)
        self.side = root / "side.log"
        self.out = root / "out.json"
        self.replays = root / "replays"
        self._mp = monkeypatch
        monkeypatch.setenv("AGENT_SIDE", str(self.side))
        monkeypatch.setenv("AGENT_OUT", str(self.out))
        for var in ("FORCE_COLOR", "COLORTERM"):
            monkeypatch.delenv(var, raising=False)

    def capsule(
        self, records: list[dict[str, Any]], overrides: list[dict[str, Any]] | None = None
    ) -> Path:
        cap = self.root / "capsule"
        cap.mkdir(exist_ok=True)
        for rec in records:
            if rec["transport"] != "mcp":  # the shared MCP builder is a minimal record
                jsonschema.validate(rec, _TOOL_SCHEMA)
        (cap / "capsule.yaml").write_text(json.dumps({
            "schema_version": "0.1.0",
            "run_id": "01HXAY7M5JZ8R7K4P9DPBYK2RN",
            "command": [sys.executable, str(self.agent)],
        }))
        (cap / "model-calls.jsonl").write_text("")
        (cap / "tool-calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
        replay_yaml = cap / "replay.yaml"
        if overrides is not None:
            replay_yaml.write_text(yaml.safe_dump(
                {"schema_version": "0.1.0", "tool_overrides": overrides}
            ))
        elif replay_yaml.exists():
            replay_yaml.unlink()
        return cap

    def replay(
        self, cap: Path, plan: list[dict[str, Any]], **flags: Any
    ) -> tuple[ReplayResult, list[dict[str, Any]]]:
        self._mp.setenv("AGENT_PLAN", json.dumps(plan))
        for stale in (self.side, self.out):
            if stale.exists():
                stale.unlink()
        result = ReplayEngine(
            capsule_dir=cap, flags=ReplayFlags(mode="mocked", **flags), base_dir=self.replays,
        ).run()
        jsonschema.validate(result.as_dict(), _RESULT_SCHEMA)
        observed = json.loads(self.out.read_text()) if self.out.exists() else []
        return result, observed

    def dry_run(self, cap: Path, **flags: Any) -> tuple[ReplayResult, str]:
        result = ReplayEngine(
            capsule_dir=cap, flags=ReplayFlags(mode="mocked", dry_run=True, **flags),
            base_dir=self.replays,
        ).run()
        return result, (self.replays / result.replay_id / "dry_run_report.txt").read_text()

    @property
    def side_effects(self) -> list[str]:
        return self.side.read_text().splitlines() if self.side.exists() else []


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workload:
    return Workload(tmp_path, monkeypatch)


def _overrides(override: bool | None, name: str) -> list[dict[str, Any]] | None:
    if override is None:
        return None
    entry: dict[str, Any] = {"tool_name": name, "allow": override}
    return [entry]


def _flags(ladder: bool, permissive: bool) -> dict[str, Any]:
    return {"allow_unknown_mutation": ladder, "permissive": permissive}


def _dry_run_label(report: str, tool_name: str) -> str:
    match = re.search(rf"\[(MOCK|LIVE|DENY|ALLOW)[^\]]*\] {re.escape(tool_name)} ", report)
    assert match, report
    return match.group(1)


def _observed_label(
    work: Workload, surface: str, result: ReplayResult, observed: list[dict[str, Any]]
) -> str:
    """What the replay actually did with the one call: LIVE (its body ran), MOCK
    (the recorded value came back and the body did not run), DENY (refused)."""
    kind = STEP_KIND[surface]
    if result.status == "aborted":
        assert result.error is not None
        assert result.error["type"] == "ToolOverrideUnenforceable"
        assert work.side_effects == [] and observed == [], "a refused replay ran something"
        return "DENY"
    ran = [line for line in work.side_effects if line.startswith(f"{kind} ")]
    (outcome,) = observed
    if ran:
        assert "value" in outcome, outcome
        return "LIVE"
    if "value" in outcome:
        assert outcome["value"] == RECORDED_VALUE[surface], outcome
        return "MOCK"
    assert outcome["error"] in (
        "ReplayToolUnmatchedError", "ReplayToolRecordNotServableError"
    ), outcome
    return "DENY"


def _expected_matched(
    surface: str, override: bool | None, ladder: bool, permissive: bool
) -> str:
    """The owner's rules for one RECORDED call, stated without the code."""
    if surface == "http":
        if override is False and not permissive:
            return "DENY"  # Q5: refuses to start
        return "LIVE"  # not intercepted: runs live, whatever replay.yaml says
    action = decide_intercepted(
        override=override,
        servable_match=surface != "python_unservable",
        permitted=ladder,  # every fixture tool is `unknown`
        permissive=permissive,
    )
    return {"serve": "MOCK", "live": "LIVE", "refuse": "DENY"}[action]


# ── dry-run == enforcement, over every override x surface x flag ─────────────


@pytest.mark.parametrize("permissive", [False, True], ids=["strict", "permissive"])
@pytest.mark.parametrize("ladder", [False, True], ids=["no-flag", "allow-unknown"])
@pytest.mark.parametrize("override", [None, False, True], ids=["none", "allow-false", "allow-true"])
@pytest.mark.parametrize("surface", ["python", "python_unservable", "mcp", "http"])
def test_dry_run_report_equals_replayed_behaviour(
    work: Workload, surface: str, override: bool | None, ladder: bool, permissive: bool
) -> None:
    """ADR-0306 D8: the label ``--dry-run`` prints for a recorded call is exactly
    what the replayed process then does with it -- one assertion over every
    path, so the two can never drift apart again (fact 8)."""
    name = TOOL_NAME[surface]
    cap = work.capsule([record_for(surface)], _overrides(override, name))
    flags = _flags(ladder, permissive)

    _, report = work.dry_run(cap, **flags)
    printed = _dry_run_label(report, name)
    result, observed = work.replay(cap, [{"kind": STEP_KIND[surface], "q": "a"}], **flags)
    actual = _observed_label(work, surface, result, observed)

    assert printed == actual, f"dry-run said {printed}, the replay did {actual}\n{report}"
    assert actual == _expected_matched(surface, override, ladder, permissive), report


# ── unmatched calls: Q3 (MCP ladder) and Q4 (asymmetric trust) ───────────────


@pytest.mark.parametrize("permissive", [False, True], ids=["strict", "permissive"])
@pytest.mark.parametrize("ladder", [False, True], ids=["no-flag", "allow-unknown"])
@pytest.mark.parametrize("override", [None, False, True], ids=["none", "allow-false", "allow-true"])
@pytest.mark.parametrize("surface", ["python", "mcp"])
def test_an_unmatched_intercepted_call_follows_the_owner_rules(
    work: Workload, surface: str, override: bool | None, ladder: bool, permissive: bool
) -> None:
    name = TOOL_NAME[surface]
    cap = work.capsule([record_for(surface, q="a")], _overrides(override, name))
    # The replayed call uses other arguments: nothing matches it.
    result, observed = work.replay(
        cap, [{"kind": STEP_KIND[surface], "q": "b"}], **_flags(ladder, permissive)
    )
    ran = bool(work.side_effects)
    if override is True and ladder:
        expected_live = True  # Q4: honoured -- re-executed, no divergence
    elif override is False:
        expected_live = False  # Q4: holds even under --permissive
    else:
        expected_live = permissive and ladder  # D7 / Q3
    assert ran is expected_live, (work.side_effects, observed, result.divergence_reason)
    contract = result.replay_contract
    assert contract is not None
    if expected_live:
        assert result.tool_calls_live == 1
    else:
        assert observed[0]["error"] == "ReplayToolUnmatchedError"
        assert result.tool_calls_live == 0
        # Strict raises at the divergence itself; `tool_refused` counts the
        # calls --permissive declined to run live.
        assert contract["tool_calls_refused"] == (1 if permissive else 0)
    honoured_reexecution = override is True and ladder
    if honoured_reexecution:
        assert result.tool_calls_unmatched == 0
    else:
        assert result.tool_calls_unmatched == 1
    if not permissive:
        # The recorded call with q="a" was never requested: strict always fails.
        assert result.status == "failure"
    elif override is False:
        assert "never run live, even under --permissive" in observed[0]["message"]


def test_an_honoured_allow_true_consumes_its_record_and_succeeds(work: Workload) -> None:
    """A re-executed call was asked for: its record is consumed, not left over."""
    for surface in ("python", "mcp"):
        cap = work.capsule([record_for(surface)], _overrides(True, TOOL_NAME[surface]))
        result, observed = work.replay(
            cap, [{"kind": STEP_KIND[surface], "q": "a"}], allow_unknown_mutation=True
        )
        assert work.side_effects == [f"{STEP_KIND[surface]} a"]
        assert "value" in observed[0]
        assert result.status == "success", result.divergence_reason
        assert result.queues_fully_consumed is True
        assert (result.tool_calls_live, result.tool_calls_mocked) == (1, 0)
        contract = result.replay_contract
        assert contract is not None
        (entry,) = contract["tool_overrides"]
        assert entry["honoured"] is True


def test_allow_true_from_the_capsule_never_runs_live_without_the_operator_flag(
    work: Workload,
) -> None:
    """Q4: the permission is reported as not honoured and the record is served."""
    cap = work.capsule(
        [record_for("mcp")],
        [{"tool_name": "search", "allow": True, "rationale": "safe lookup"}],
    )
    result, observed = work.replay(cap, [{"kind": "mcp", "q": "a"}], allow_readonly=True)
    assert work.side_effects == []
    assert observed == [{"value": "recorded"}]
    assert result.status == "success"
    contract = result.replay_contract
    assert contract is not None
    (entry,) = contract["tool_overrides"]
    assert entry["honoured"] is False
    assert entry["reason"].startswith("override_not_honoured")
    assert "--allow-unknown-mutation" in entry["reason"]
    assert entry["rationale"] == "safe lookup"


def test_a_replay_without_overrides_writes_no_override_report(work: Workload) -> None:
    """Absent is not present-but-empty: the key appears only with overrides."""
    result, _ = work.replay(work.capsule([record_for("mcp")]), [{"kind": "mcp", "q": "a"}])
    assert result.status == "success"
    assert "tool_overrides" not in (result.replay_contract or {})


# ── Q5: refuse to start when an `allow: false` override cannot be enforced ───


def _cli(*args: str) -> Any:
    return CliRunner().invoke(app, ["replay", *args])


def test_strict_replay_refuses_to_start_on_an_unenforceable_override(work: Workload) -> None:
    cap = work.capsule([record_for("http")], [{"tool_name": "fetch", "allow": False}])
    work._mp.setenv("AGENT_PLAN", json.dumps([{"kind": "http", "q": "a"}]))
    out = _cli(str(cap), "-o", str(work.replays))
    assert out.exit_code == 3, out.output
    assert not work.side.exists() and not work.out.exists(), "the workload was started"
    (replay_dir,) = work.replays.iterdir()
    result = yaml.safe_load((replay_dir / "replay_result.yaml").read_text())
    jsonschema.validate(result, _RESULT_SCHEMA)
    assert result["status"] == "aborted"
    assert result["error"]["type"] == "ToolOverrideUnenforceable"
    assert "'fetch' (transport http)" in result["error"]["message"]
    assert "ToolOverrideUnenforceable" in strip_ansi(out.output)

    dry = _cli(str(cap), "--dry-run", "-o", str(work.replays))
    assert dry.exit_code == 3, dry.output
    assert "REFUSED:" in strip_ansi(dry.output)


def test_permissive_starts_and_reports_an_unenforceable_override(work: Workload) -> None:
    cap = work.capsule([record_for("http")], [{"tool_name": "fetch", "allow": False}])
    work._mp.setenv("AGENT_PLAN", json.dumps([{"kind": "http", "q": "a"}]))
    dry = _cli(str(cap), "--dry-run", "--permissive", "-o", str(work.replays / "dry"))
    assert dry.exit_code == 0, dry.output
    assert "WARNING (override_unenforceable" in strip_ansi(dry.output)

    out = _cli(str(cap), "--permissive", "-o", str(work.replays))
    assert out.exit_code == 0, out.output
    assert work.side_effects == ["http a"], "the tool runs live under --permissive"
    (replay_dir,) = [p for p in work.replays.iterdir() if p.name != "dry"]
    result = yaml.safe_load((replay_dir / "replay_result.yaml").read_text())
    assert result["status"] == "success"
    kinds = [d["kind"] for d in result["replay_contract"]["divergences"]]
    assert kinds[0] == "override_unenforceable"
    assert "override_unenforceable" in result["divergence_reason"]
    (entry,) = result["replay_contract"]["tool_overrides"]
    assert entry["honoured"] is False


def test_an_override_naming_an_intercepted_tool_does_not_refuse_to_start(
    work: Workload,
) -> None:
    """Red check for Q5: the same override on an MCP record is enforceable."""
    cap = work.capsule([record_for("mcp")], [{"tool_name": "search", "allow": False}])
    work._mp.setenv("AGENT_PLAN", json.dumps([{"kind": "mcp", "q": "a"}]))
    out = _cli(str(cap), "-o", str(work.replays))
    assert out.exit_code == 0, out.output


def test_dry_run_lists_every_override_and_whether_it_is_honoured(work: Workload) -> None:
    cap = work.capsule(
        [record_for("mcp"), record_for("python")],
        [
            {"tool_name": "search", "allow": False},
            {"tool_name": "lookup", "allow": True},
            {"tool_name": "absent_tool", "allow": False},
        ],
    )
    result, report = work.dry_run(cap)
    assert result.error is None
    assert "Tool overrides (replay.yaml):" in report
    assert re.search(r"search\s+allow=false\s+honoured=yes", report), report
    assert re.search(r"lookup\s+allow=true\s+honoured=no\s+\(override_not_honoured", report)
    assert re.search(r"absent_tool\s+allow=false\s+honoured=no\s+\(override_unused", report)


# ── the dispatcher must exist for `allow: false` to hold ─────────────────────


def test_a_failed_install_stops_a_permissive_replay_with_a_deny_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under --permissive a failed install used to let the workload run with no
    dispatcher at all -- every tool live, `allow: false` included."""
    from novafabric.replay import _dispatcher

    exits: list[int] = []

    def fake_exit(code: int) -> None:
        exits.append(code)

    (tmp_path / "model.json").write_text("[]")
    (tmp_path / "tools.json").write_text("[]")
    (tmp_path / "policy.json").write_text("{not json")
    class _NoModelDispatcher:  # keep this process's SDKs unpatched
        installed_surfaces: list[str] = []

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def install(self) -> None:
            pass

    monkeypatch.setattr(_dispatcher, "MockModelDispatcher", _NoModelDispatcher)
    monkeypatch.setattr(_dispatcher.os, "_exit", fake_exit)
    monkeypatch.setenv("NOVAFABRIC_REPLAY_QUEUE_PATH", str(tmp_path / "model.json"))
    monkeypatch.setenv("NOVAFABRIC_REPLAY_TOOL_QUEUE_PATH", str(tmp_path / "tools.json"))
    monkeypatch.setenv("NOVAFABRIC_REPLAY_TOOL_POLICY_PATH", str(tmp_path / "policy.json"))
    monkeypatch.setenv("NOVAFABRIC_REPLAY_DIVERGENCE_POLICY", "warn")
    monkeypatch.delenv("NOVAFABRIC_REPLAY_EVENTS_PATH", raising=False)

    monkeypatch.delenv("NOVAFABRIC_REPLAY_INSTALL_REQUIRED", raising=False)
    _dispatcher.install_from_env()
    assert exits == []  # permissive, nothing that must hold: unchanged

    monkeypatch.setenv("NOVAFABRIC_REPLAY_INSTALL_REQUIRED", "1")
    _dispatcher.install_from_env()
    assert exits == [_dispatcher.REPLAY_DISPATCHER_UNAVAILABLE_EXIT]


def test_cli_help_documents_the_override_rules() -> None:
    result = CliRunner().invoke(app, ["replay", "--help"])
    assert result.exit_code == 0
    assert_flag_in_help(result, "--permissive")
    text = " ".join(strip_ansi(result.output).split())
    assert "ToolOverrideUnenforceable" in text
    assert "allow: false" in text
