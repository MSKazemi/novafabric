"""ADR-0306 Q7 (Option 1), end to end with real LangGraph: an adapter capsule is
replayed by re-running the process invocation the adapter recorded.

The workload is the shape of the c07 ``code_reviewer`` bench workload: a script
run as ``python agent.py <input>`` that builds a LangGraph ``StateGraph`` (agent
node -> ``ToolNode`` -> agent) and wraps it with ``novafabric.adapters.langgraph.wrap``
-- **no** ``nova capture`` prefix; the adapter writes the capsule. The agent node
calls the real ``openai`` SDK over an ``httpx.MockTransport`` that serves canned
responses at capture and raises if it is reached during replay. No network, no key.

Independent oracle: the workload writes its final answer to a file outside the
capsule; the replay must reproduce it from the served responses.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from _mocked_replay_agent import openai_body
from typer.testing import CliRunner

from novafabric.cli.main import app

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")
pytest.importorskip("openai")
pytest.importorskip("httpx")

runner = CliRunner()
REPO = Path(__file__).resolve().parents[2]
_RESULT_SCHEMA = json.loads(
    (REPO / "src" / "novafabric" / "schemas" / "replay-result.schema.json").read_text()
)
_CAPSULE_SCHEMA = json.loads(
    (REPO / "src" / "novafabric" / "schemas" / "run-capsule.schema.json").read_text()
)

SENTINEL = "SENTINEL-q7-5be1"

AGENT = '''
import json, os, sys
from pathlib import Path

import httpx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from openai import OpenAI

from novafabric.adapters.langgraph import wrap

HERE = Path(__file__).parent
RUNS = Path(os.environ["AGENT_RUNS"])
OUT = Path(os.environ["AGENT_OUT"])
CANNED = json.loads(os.environ.get("AGENT_CANNED") or "null")
SERVED = [0]


def transport(request):
    if CANNED is None:
        raise RuntimeError("network reached during replay")
    body = CANNED[SERVED[0]]
    SERVED[0] += 1
    return httpx.Response(200, json=body)


client = OpenAI(api_key="sk-test", max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(transport)))


SIDE = os.environ.get("AGENT_SIDE")


@tool
def read_snippet(path: str) -> str:
    """Read a snippet for review."""
    if SIDE:
        with open(SIDE, "a") as fh:
            fh.write(f"read {path}\\n")
    return (HERE / path).read_text()


TOOLS = [read_snippet]
if os.environ.get("AGENT_WRAP_TOOLS"):
    from novafabric.adapters.langchain_tools import wrap_tools

    TOOLS = wrap_tools(TOOLS, {"read_snippet": "read-only"})
SPECS = [{"type": "function", "function": {
    "name": t.name, "description": t.description,
    "parameters": t.tool_call_schema.model_json_schema()}} for t in TOOLS]


def to_openai(m):
    if isinstance(m, HumanMessage):
        return {"role": "user", "content": m.content}
    if isinstance(m, ToolMessage):
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
    return {"role": "assistant", "content": m.content or None, "tool_calls": [
        {"id": c["id"], "type": "function",
         "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
        for c in m.tool_calls] or None}


def agent(state):
    r = client.chat.completions.create(
        model="gpt-4o", tools=SPECS, messages=[to_openai(m) for m in state["messages"]])
    msg = r.choices[0].message
    calls = [{"name": c.function.name, "args": json.loads(c.function.arguments), "id": c.id}
             for c in (msg.tool_calls or [])]
    return {"messages": [AIMessage(msg.content or "", tool_calls=calls)]}


graph = StateGraph(MessagesState)
graph.add_node("agent", agent)
graph.add_node("tools", ToolNode(TOOLS))
graph.add_edge(START, "agent")
graph.add_conditional_edges("agent", tools_condition)
graph.add_edge("tools", "agent")
APP = wrap(graph.compile(), run_name="code-reviewer", data_dir=RUNS)


def main(snippet):
    answers = []
    for _ in range(int(os.environ.get("AGENT_RUNS_COUNT", "1"))):
        result = APP.invoke({"messages": [HumanMessage(f"Review {snippet}")]})
        answers.append(result["messages"][-1].content)
    OUT.write_text(json.dumps(answers))
    print("review:", answers[-1])


main(sys.argv[1] if len(sys.argv) > 1 else "snippet_a.py")
'''


def _canned(n_runs: int = 1) -> list[dict[str, Any]]:
    one = [
        openai_body(tool_calls=[("call_R", "read_snippet", {"path": "snippet_a.py"})], rid="r1"),
        openai_body(content=f"verdict: {SENTINEL}", rid="r2"),
    ]
    return one * n_runs


class Bench:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.work = root / "work"
        self.work.mkdir()
        (self.work / "agent.py").write_text(AGENT)
        (self.work / "snippet_a.py").write_text("def f(x): return x / 0\n")
        self.runs = root / "runs"
        self.out = root / "out.json"
        self.replays = root / "replays"
        self.nf_home = root / "nf-home"
        self._mp = monkeypatch
        monkeypatch.setenv("AGENT_RUNS", str(self.runs))
        monkeypatch.setenv("AGENT_OUT", str(self.out))
        monkeypatch.setenv("NOVAFABRIC_HOME", str(self.nf_home))
        monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(root / "audit.log"))
        monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "forensic")
        monkeypatch.setenv("LANGSMITH_TRACING", "false")
        for var in ("NOVAFABRIC_CAPSULE_DIR", "AGENT_CANNED", "AGENT_RUNS_COUNT",
                    "AGENT_WRAP_TOOLS", "AGENT_SIDE",
                    "LANGCHAIN_TRACING_V2", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY",
                    "FORCE_COLOR", "COLORTERM"):
            monkeypatch.delenv(var, raising=False)

    def capture(self, *, runs: int = 1, args: tuple[str, ...] = ("snippet_a.py",)) -> list[Path]:
        env = dict(os.environ)
        env["AGENT_CANNED"] = json.dumps(_canned(runs))
        env["AGENT_RUNS_COUNT"] = str(runs)
        # `python agent.py <input>` in the workload's directory, as a user runs it.
        proc = subprocess.run(
            [sys.executable, "agent.py", *args], cwd=self.work, env=env,
            capture_output=True, text=True, timeout=300,
        )
        assert proc.returncode == 0, proc.stderr
        # The replayed process gets the same program inputs, minus the canned
        # model responses (those must come from the capsule).
        self._mp.setenv("AGENT_RUNS_COUNT", str(runs))
        capsules = sorted(
            (p for p in self.runs.iterdir() if (p / "capsule.yaml").exists()),
            key=lambda p: yaml.safe_load((p / "capsule.yaml").read_text())["created_at"],
        )
        assert len(capsules) == runs
        return capsules

    def answers(self) -> Any:
        if not self.out.exists():
            return None
        data = json.loads(self.out.read_text())
        self.out.unlink()
        return data

    def replay(self, capsule: Path, *flags: str) -> tuple[dict[str, Any], Any]:
        before = set(self.replays.iterdir()) if self.replays.exists() else set()
        out = runner.invoke(app, ["replay", str(capsule), "-o", str(self.replays), *flags],
                            env={"COLUMNS": "400"})
        (replay_dir,) = set(self.replays.iterdir()) - before
        result = yaml.safe_load((replay_dir / "replay_result.yaml").read_text())
        jsonschema.validate(result, _RESULT_SCHEMA)
        result["_cli_exit"] = out.exit_code
        return result, self.answers()

    def capsule_count(self) -> int:
        return sum(1 for p in self.runs.iterdir() if (p / "capsule.yaml").exists())


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Bench:
    # The test process is not where `nova replay` should look for the program:
    # the replay must use the recorded cwd, not this one.
    monkeypatch.chdir(tmp_path)
    return Bench(tmp_path, monkeypatch)


def _check(result: dict[str, Any], name: str) -> dict[str, Any]:
    (check,) = [c for c in result["fidelity"]["checks"] if c["name"] == name]
    return check


def test_a_langgraph_adapter_capsule_records_its_process_invocation(bench: Bench) -> None:
    (capsule,) = bench.capture()
    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    jsonschema.validate(manifest, _CAPSULE_SCHEMA)
    assert manifest["command"] == ["@langgraph:code-reviewer"]
    assert manifest["capture_mode"] == "sdk-decorator"
    facet = manifest["facets"]["process_invocation"]
    assert facet["argv"][1:] == ["agent.py", "snippet_a.py"]
    assert facet["entry_kind"] == "script"
    assert facet["entry"] == "agent.py"
    assert facet["adapter_run_ordinal"] == 1
    assert facet["host_modules"] == []
    assert Path(os.path.expanduser(facet["cwd"])) == bench.work
    assert manifest["model_call_count"] == 2


def test_mocked_replay_reruns_the_script_and_serves_its_model_calls(bench: Bench) -> None:
    (capsule,) = bench.capture()
    captured = bench.answers()
    assert captured == [f"verdict: {SENTINEL}"]

    result, replayed = bench.replay(capsule)  # AGENT_CANNED unset: no network
    assert result["status"] == "success", result.get("error")
    assert result["_cli_exit"] == 0
    assert replayed == captured  # the served answer, by an oracle outside the capsule
    assert result["model_calls_mocked"] == 2
    assert result["model_calls_unmatched"] == 0
    assert result["queues_fully_consumed"] is True
    assert result["replay_contract"]["divergences"] == []
    # The verdict compares what the capsule recorded: the framework call's outcome
    # and the served call sequence. The process output was never recorded by an
    # adapter, so the verdict cannot be `faithful` -- it is `not_compared`.
    fidelity = result["fidelity"]
    exit_check = _check(result, "exit_code")
    assert exit_check["equal"] is True
    assert "framework call" in exit_check["note"]
    assert _check(result, "model_call_sequence")["equal"] is True
    assert _check(result, "divergence")["equal"] is True
    assert _check(result, "live_calls")["equal"] is True
    assert _check(result, "stdout_sha256")["equal"] is None
    assert fidelity["verdict"] == "not_compared"
    # Replaying wrote no new capsule: not into the workload's store, not into the
    # isolated capsule home.
    assert bench.capsule_count() == 1
    assert not (bench.nf_home / "runs").exists() or not any((bench.nf_home / "runs").iterdir())


def test_wrapped_langchain_tools_are_recorded_and_served_in_an_adapter_capsule(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ADR-0306 slice 3 inside an adapter capsule: the ToolNode tool goes through
    ``langchain_tools.wrap_tools``, its call is recorded into the adapter's capsule,
    and the re-run process is served the recorded result -- the body never runs."""
    side = tmp_path / "side.log"
    monkeypatch.setenv("AGENT_WRAP_TOOLS", "1")
    monkeypatch.setenv("AGENT_SIDE", str(side))
    (capsule,) = bench.capture()
    captured = bench.answers()
    assert side.read_text().splitlines() == ["read snippet_a.py"]
    side.unlink()
    records = [
        json.loads(line)
        for line in (capsule / "tool-calls.jsonl").read_text().splitlines() if line.strip()
    ]
    assert [r["tool_name"] for r in records] == ["read_snippet"]

    result, replayed = bench.replay(capsule)
    assert result["status"] == "success", result.get("error")
    assert replayed == captured
    assert not side.exists(), "the wrapped tool body ran during mocked replay"
    assert result["tool_calls_mocked"] == 1
    assert result["model_calls_mocked"] == 2
    assert result["queues_fully_consumed"] is True
    assert _check(result, "tool_call_sequence")["equal"] is True


def test_without_the_facet_the_same_capsule_is_refused_as_before(bench: Bench) -> None:
    """Red check: the facet is what makes the replay possible."""
    (capsule,) = bench.capture()
    bench.answers()
    path = capsule / "capsule.yaml"
    manifest = yaml.safe_load(path.read_text())
    del manifest["facets"]
    path.write_text(yaml.safe_dump(manifest))
    result, replayed = bench.replay(capsule)
    assert result["status"] == "aborted"
    assert result["_cli_exit"] == 1
    assert result["error"]["type"] == "CapsuleNotReplayable"
    assert "open question 7" in result["error"]["message"]
    assert replayed is None  # nothing ran


def test_an_entry_script_edited_after_capture_is_refused(bench: Bench) -> None:
    (capsule,) = bench.capture()
    bench.answers()
    script = bench.work / "agent.py"
    script.write_text(script.read_text() + "\n# edited\n")
    result, replayed = bench.replay(capsule)
    assert result["status"] == "aborted"
    assert result["error"]["guard"] == "entry_changed"
    assert replayed is None


def test_a_second_adapter_run_in_the_replayed_process_diverges(bench: Bench) -> None:
    first, second = bench.capture(runs=2)
    bench.answers()
    # The second run's capsule is refused up front: v1 re-runs ordinal 1 only.
    result, replayed = bench.replay(second)
    assert result["status"] == "aborted"
    assert result["error"]["guard"] == "not_first_adapter_run"
    assert replayed is None

    # The first run's capsule re-runs the whole process, which makes a second
    # adapter run the capsule never recorded: fail-closed.
    result, _ = bench.replay(first)
    assert result["status"] == "failure"
    kinds = [d["kind"] for d in result["replay_contract"]["divergences"]]
    assert "adapter_run_count_mismatch" in kinds
    assert result["fidelity"]["verdict"] in ("diverged", "failed")
    assert bench.capsule_count() == 2  # no capsule written during the replay
