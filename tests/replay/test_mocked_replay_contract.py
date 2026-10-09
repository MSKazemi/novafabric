"""ADR-0300 / issue #16: the mocked-replay contract, scenario by scenario.

Each end-to-end test writes a synthetic capsule, then lets ``ReplayEngine`` re-run
a real agent process (``tests/_mocked_replay_agent.py``): the real ``openai`` SDK
with the network refused, and a real in-memory MCP server whose tools append to a
side-effect log. Scenario numbers refer to the acceptance list in issue #16.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from _help_assert import assert_flag_in_help
from _mocked_replay_agent import (
    mcp_record,
    openai_body,
    openai_record,
    write_agent,
    write_capsule,
    write_fake_anthropic,
)
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.replay import ReplayEngine, ReplayFlags
from novafabric.replay._contract import (
    ToolCallMatcher,
    interceptable_tool_calls,
    normalized_arg_hash,
    summarize,
)
from novafabric.replay._result import ReplayResult

pytest.importorskip("openai")
pytest.importorskip("mcp")

_SCHEMA = json.loads(
    (Path(__file__).parents[2] / "schemas" / "replay-result.schema.json").read_text()
)


class Agent:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.path = write_agent(root)
        self.out = root / "out.json"
        self.side = root / "side.log"
        self._mp = monkeypatch
        monkeypatch.setenv("AGENT_OUT", str(self.out))
        monkeypatch.setenv("AGENT_SIDE", str(self.side))
        for var in ("AGENT_CANNED", "FAKE_ANTHROPIC_CANNED"):
            monkeypatch.delenv(var, raising=False)

    def replay(
        self,
        model_calls: list[dict[str, Any]],
        plan: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]] = (),  # type: ignore[assignment]
        *,
        permissive: bool = False,
        command: list[str] | None = None,
    ) -> tuple[ReplayResult, list[dict[str, Any]]]:
        self._mp.setenv("AGENT_PLAN", json.dumps(plan))
        cap = write_capsule(self.root, self.path, model_calls, tool_calls, command)
        result = ReplayEngine(
            capsule_dir=cap,
            flags=ReplayFlags(mode="mocked", permissive=permissive),
            base_dir=self.root / "replays",
        ).run()
        jsonschema.validate(result.as_dict(), _SCHEMA)
        observed = json.loads(self.out.read_text()) if self.out.exists() else []
        return result, observed

    @property
    def side_effects(self) -> list[str]:
        return self.side.read_text().splitlines() if self.side.exists() else []


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Agent:
    return Agent(tmp_path, monkeypatch)


def _kinds(result: ReplayResult) -> list[str]:
    assert result.replay_contract is not None
    return [d["kind"] for d in result.replay_contract["divergences"]]


def _tc(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"id": call_id, "name": name, "arguments": args}


TOOL = {"op": "tool", "name": "get_weather", "args": {"city": "Paris"}}


# ── scenario 1: one model call, no tools ─────────────────────────────────────


def test_s1_one_model_call_no_tools(agent: Agent) -> None:
    result, obs = agent.replay([openai_record("hello")], [{"op": "chat"}])
    assert obs[0]["content"] == "hello"
    assert result.status == "success"
    assert (result.model_calls_mocked, result.model_calls_available) == (1, 1)
    assert result.model_calls_unmatched == 0
    assert result.tool_calls_mocked == 0
    assert result.queues_fully_consumed is True
    assert result.divergence_reason is None


# ── scenarios 2-5: tool-result substitution, one-to-one ──────────────────────


def test_s2_model_tool_model(agent: Agent) -> None:
    result, obs = agent.replay(
        [openai_record(None, [_tc("call_1", "get_weather", {"city": "Paris"})]),
         openai_record("Paris is 22C")],
        [{"op": "chat"}, TOOL, {"op": "chat"}],
        [mcp_record("get_weather", {"city": "Paris"}, "Paris: 22C")],
    )
    assert obs[1]["text"] == "Paris: 22C"  # the recorded result, not "Paris: live"
    assert obs[2]["content"] == "Paris is 22C"
    assert agent.side_effects == []
    assert result.status == "success"
    assert (result.tool_calls_mocked, result.tool_calls_available) == (1, 1)
    assert result.tool_calls_live == 0
    assert result.queues_fully_consumed is True


def test_s3_multiple_sequential_tools(agent: Agent) -> None:
    tools = [
        mcp_record("get_weather", {"city": "Paris"}, "P", tool_call_id="01HXAY7M5JZ8R7K4P9DPBYK2T1"),
        mcp_record("write_file", {"path": "a", "text": "b"}, "W", tool_call_id="01HXAY7M5JZ8R7K4P9DPBYK2T2"),
        mcp_record("fetch_url", {"url": "https://x"}, "F", tool_call_id="01HXAY7M5JZ8R7K4P9DPBYK2T3"),
    ]
    plan = [
        TOOL,
        {"op": "tool", "name": "write_file", "args": {"path": "a", "text": "b"}},
        {"op": "tool", "name": "fetch_url", "args": {"url": "https://x"}},
    ]
    result, obs = agent.replay([], plan, tools)
    assert [o["text"] for o in obs] == ["P", "W", "F"]
    assert agent.side_effects == []
    assert result.status == "success"
    assert result.tool_calls_mocked == 3


def test_s4_repeated_identical_calls_consume_distinct_records(agent: Agent) -> None:
    tools = [
        mcp_record("get_weather", {"city": "Paris"}, "first", tool_call_id="01HXAY7M5JZ8R7K4P9DPBYK2T1"),
        mcp_record("get_weather", {"city": "Paris"}, "second", tool_call_id="01HXAY7M5JZ8R7K4P9DPBYK2T2"),
    ]
    result, obs = agent.replay([], [TOOL, TOOL, {**TOOL, "catch": True}], tools)
    # one-to-one: never the same record twice, in recorded order ...
    assert [o.get("text") for o in obs[:2]] == ["first", "second"]
    # ... and a third identical call has nothing left to consume.
    assert obs[2]["error"] == "ReplayToolUnmatchedError"
    assert "already consumed" in obs[2]["message"]
    assert agent.side_effects == []
    assert result.status == "failure"
    assert result.tool_calls_unmatched == 1


def test_s5_same_tool_different_arguments_match_by_arguments(agent: Agent) -> None:
    tools = [
        mcp_record("get_weather", {"city": "Paris"}, "P", tool_call_id="01HXAY7M5JZ8R7K4P9DPBYK2T1"),
        mcp_record("get_weather", {"city": "Rome"}, "R", tool_call_id="01HXAY7M5JZ8R7K4P9DPBYK2T2"),
    ]
    plan = [{"op": "tool", "name": "get_weather", "args": {"city": "Rome"}}, TOOL]
    result, obs = agent.replay([], plan, tools)
    assert [o["text"] for o in obs] == ["R", "P"]
    assert result.status == "success"


# ── scenarios 6-7: matcher identity rules ────────────────────────────────────


def _records() -> list[dict[str, Any]]:
    return [
        mcp_record("search", {"q": "a"}, "A", tool_call_id="id-1"),
        mcp_record("search", {"q": "a"}, "A2", tool_call_id="id-2"),
    ]


def test_s6_preserved_tool_call_id_wins() -> None:
    matcher = ToolCallMatcher(_records())
    match = matcher.match("id-2", "search", {"q": "a"})
    assert match.how == "id"
    assert match.record is not None and match.record["tool_call_id"] == "id-2"
    # the id-matched record is consumed; the next identical call gets the other one
    assert matcher.match(None, "search", {"q": "a"}).record["tool_call_id"] == "id-1"  # type: ignore[index]
    assert matcher.match(None, "search", {"q": "a"}).record is None


def test_s7_changed_id_falls_back_to_name_and_arguments_in_sequence() -> None:
    matcher = ToolCallMatcher(_records())
    first = matcher.match("fresh-id", "search", {"q": "a"})
    second = matcher.match("fresh-id-2", "search", {"q": "a"})
    assert (first.how, second.how) == ("signature", "signature")
    assert [first.record["tool_call_id"], second.record["tool_call_id"]] == ["id-1", "id-2"]  # type: ignore[index]


def test_id_recorded_for_another_tool_is_unmatched_not_guessed() -> None:
    match = ToolCallMatcher(_records()).match("id-1", "delete_everything", {"q": "a"})
    assert match.record is None
    assert "recorded for 'search'" in str(match.reason)


def test_argument_hash_is_order_insensitive_and_never_collapses_non_dicts() -> None:
    assert normalized_arg_hash({"a": 1, "b": 2}) == normalized_arg_hash({"b": 2, "a": 1})
    assert normalized_arg_hash(None) == normalized_arg_hash({})
    assert normalized_arg_hash("x") != normalized_arg_hash({})
    assert normalized_arg_hash([1]) != normalized_arg_hash([2])


# ── scenario 8: recorded tool response missing ───────────────────────────────


def test_s8_missing_tool_record_fails_closed_even_if_the_workload_swallows_it(
    agent: Agent,
) -> None:
    result, obs = agent.replay([], [{**TOOL, "catch": True}])
    assert obs[0]["error"] == "ReplayToolUnmatchedError"
    assert agent.side_effects == [], "the live tool must not run in strict mode"
    assert result.exit_code == 0  # the agent caught the exception ...
    assert result.status == "failure"  # ... the replay still fails
    assert result.error == {"type": "ReplayDivergence", "message": result.divergence_reason}
    assert "tool_call_unmatched" in str(result.divergence_reason)
    assert result.tool_calls_unmatched == 1


# ── scenario 9: extra model call after the queue is exhausted ────────────────


def test_s9_extra_model_call_fails_closed_with_details(agent: Agent) -> None:
    result, obs = agent.replay([openai_record("one")], [{"op": "chat"}, {"op": "chat", "catch": True}])
    assert obs[0]["content"] == "one"
    assert obs[1]["error"] == "ReplayQueueExhaustedError"
    assert result.status == "failure"
    (divergence,) = result.replay_contract["divergences"]  # type: ignore[index]
    assert divergence["kind"] == "model_queue_exhausted"
    assert divergence["provider"] == "openai"
    assert divergence["call_index"] == 1
    assert divergence["recorded_queue_length"] == 1
    assert (result.model_calls_mocked, result.model_calls_unmatched) == (1, 1)


def test_s9_permissive_keeps_the_labelled_warn_and_empty_behaviour(agent: Agent) -> None:
    result, obs = agent.replay(
        [openai_record("one")], [{"op": "chat"}, {"op": "chat", "catch": True}], permissive=True
    )
    # pre-ADR-0300 behaviour: an empty response (no choices) reaches the agent
    assert obs[1]["error"] == "IndexError"
    assert result.status == "success"
    assert result.policy_flags_used == ["--permissive"]
    assert result.replay_contract["divergence_policy"] == "warn"  # type: ignore[index]
    assert "model_queue_exhausted" in str(result.divergence_reason)
    assert result.model_calls_unmatched == 1


# ── scenario 10: fewer model calls than recorded ─────────────────────────────


def test_s10_unconsumed_recorded_calls_fail_strict_replay(agent: Agent) -> None:
    result, _ = agent.replay([openai_record("one"), openai_record("two")], [{"op": "chat"}])
    assert result.exit_code == 0
    assert result.status == "failure"
    assert result.queues_fully_consumed is False
    assert _kinds(result) == ["model_calls_unconsumed"]
    assert result.replay_contract["model_calls_unconsumed"] == 1  # type: ignore[index]


def test_s10_permissive_reports_unconsumed_without_failing(agent: Agent) -> None:
    result, _ = agent.replay(
        [openai_record("one"), openai_record("two")], [{"op": "chat"}], permissive=True
    )
    assert result.status == "success"
    assert result.queues_fully_consumed is False
    assert "model_calls_unconsumed" in str(result.divergence_reason)


# ── scenario 11: a different provider / order than recorded ──────────────────


def test_s11_provider_mismatch(
    agent: Agent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(write_fake_anthropic(tmp_path / "fake_sdk")))
    result, obs = agent.replay([openai_record("x")], [{"op": "anthropic", "catch": True}])
    assert obs[0]["error"] == "ReplayProviderMismatchError"
    assert result.status == "failure"
    assert _kinds(result) == ["provider_mismatch", "model_calls_unconsumed"]


def test_s11_cross_provider_order_mismatch(
    agent: Agent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(write_fake_anthropic(tmp_path / "fake_sdk")))
    records = [openai_record("o"), openai_record("a", system="anthropic")]
    result, obs = agent.replay(records, [{"op": "anthropic", "catch": True}, {"op": "chat"}])
    assert obs[0]["error"] == "ReplayOrderMismatchError"
    assert result.status == "failure"
    assert "order_mismatch" in _kinds(result)


def test_s11_a_chat_recording_is_not_served_to_the_responses_api(agent: Agent) -> None:
    """ADR-0304: one queue per API surface -- a Chat Completions recording is
    never reshaped into a Responses API reply."""
    result, obs = agent.replay([openai_record("x")], [{"op": "responses", "catch": True}])
    assert obs[0]["error"] == "ReplayProviderMismatchError"
    assert "openai.responses.create" in obs[0]["message"]
    assert "openai.chat.completions.create" in obs[0]["message"]
    assert result.status == "failure"
    assert _kinds(result) == ["provider_mismatch", "model_calls_unconsumed"]


def test_s11_cross_surface_order_mismatch(agent: Agent) -> None:
    records = [openai_record("chat"),
               openai_record("resp", surface="openai.responses")]
    result, obs = agent.replay(
        records, [{"op": "responses", "catch": True}, {"op": "chat"}]
    )
    assert obs[0]["error"] == "ReplayOrderMismatchError"
    assert result.status == "failure"
    (mismatch,) = [
        d for d in result.replay_contract["divergences"]  # type: ignore[index]
        if d["kind"] == "order_mismatch"
    ]
    assert mismatch["surface"] == "openai.responses.create"
    assert mismatch["expected_surface"] == "openai.chat.completions.create"


# ── scenarios 12-13: mutating and network tools under strict mode ────────────


def test_s12_mutating_tool_is_served_from_the_capsule_or_refused_never_run(
    agent: Agent,
) -> None:
    recorded = mcp_record(
        "write_file", {"path": "a", "text": "b"}, "written (recorded)",
        mutation_class="non-idempotent-write",
    )
    plan = [
        {"op": "tool", "name": "write_file", "args": {"path": "a", "text": "b"}},
        {"op": "tool", "name": "write_file", "args": {"path": "a", "text": "CHANGED"},
         "catch": True},
    ]
    result, obs = agent.replay([], plan, [recorded])
    assert obs[0]["text"] == "written (recorded)"
    assert obs[1]["error"] == "ReplayToolUnmatchedError"
    assert "not with these arguments" in obs[1]["message"]
    assert agent.side_effects == [], "a mutating tool executed during strict mocked replay"
    assert result.status == "failure"
    assert (result.tool_calls_mocked, result.tool_calls_unmatched, result.tool_calls_live) == (1, 1, 0)


def test_s13_network_tool_refused_and_uncontrolled_transports_reported(agent: Agent) -> None:
    http_tool = {**mcp_record("fetch", {"u": 1}, "x"), "transport": "http", "mcp": None}
    http_tool.pop("mcp")
    result, obs = agent.replay(
        [], [{"op": "tool", "name": "fetch_url", "args": {"url": "https://evil"}, "catch": True}],
        [http_tool],
    )
    assert obs[0]["error"] == "ReplayToolUnmatchedError"
    assert agent.side_effects == []
    assert result.status == "failure"
    assert result.tool_calls_recorded == 1
    assert result.tool_calls_available == 0
    assert result.replay_contract["tool_calls_not_interceptable"] == 1  # type: ignore[index]


def test_s13_live_network_from_an_uncontrolled_tool_is_reported_not_blocked(
    agent: Agent,
) -> None:
    """ADR-0304: replay cannot intercept a plain socket "tool", but it reports that
    the replay reached the network -- so ``success`` never implies "offline"."""
    result, obs = agent.replay([openai_record("x")], [{"op": "chat"}, {"op": "net"}])
    assert obs[1] == {"op": "net"}
    assert result.status == "success"  # observed, never blocked
    contract = result.replay_contract
    assert contract is not None
    assert contract["network_observed"] is True
    assert contract["network_connections_live"] == 1
    (destination,) = contract["network_destinations"]
    assert destination.startswith("127.0.0.1:")


def test_a_replay_with_no_network_reports_zero_connections(agent: Agent) -> None:
    result, _ = agent.replay([openai_record("x")], [{"op": "chat"}])
    assert result.replay_contract is not None
    assert result.replay_contract["network_observed"] is True
    assert result.replay_contract["network_connections_live"] == 0
    assert result.replay_contract["network_destinations"] == []


def test_s13_permissive_runs_an_unmatched_tool_live_and_counts_it(agent: Agent) -> None:
    result, obs = agent.replay(
        [], [{"op": "tool", "name": "fetch_url", "args": {"url": "https://x"}}], permissive=True
    )
    assert obs[0]["text"] == "fetched"
    assert agent.side_effects == ["fetch_url https://x"]
    assert result.status == "success"
    assert (result.tool_calls_live, result.tool_calls_unmatched) == (1, 1)
    assert "tool_call_unmatched" in str(result.divergence_reason)


# ── scenario 17: unsupported surfaces are refused, not run live ──────────────


def test_s17_unsupported_model_surfaces_are_refused(
    agent: Agent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0304 serves async, streaming and the Responses API; what is still not
    served is refused under strict replay instead of reaching the network."""
    monkeypatch.setenv("PYTHONPATH", str(write_fake_anthropic(tmp_path / "fake_sdk")))
    plan = [
        {"op": "parse", "catch": True},
        {"op": "legacy", "catch": True},
        {"op": "raw", "catch": True},
        {"op": "anthropic_stream_helper", "catch": True},
    ]
    result, obs = agent.replay([], plan)
    assert [o["error"] for o in obs] == ["ReplayUnsupportedSurfaceError"] * 4
    assert result.status == "failure"
    assert _kinds(result) == ["unsupported_surface"] * 4
    surfaces = [d["surface"] for d in result.replay_contract["divergences"]]  # type: ignore[index]
    assert surfaces == [
        "openai.chat.completions.parse (structured outputs)",
        "openai.completions.create (legacy text completions)",
        "openai.chat.completions.create via with_raw_response / with_streaming_response",
        "anthropic.messages.stream",
    ]
    assert result.model_calls_unmatched == 4
    assert result.replay_contract["model_calls_live"] == 0  # type: ignore[index]


def test_s17_permissive_lets_an_unsupported_surface_run_live_and_counts_it(
    agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_CANNED", json.dumps([openai_body(content="live answer")]))
    result, obs = agent.replay([], [{"op": "raw"}], permissive=True)
    assert "error" not in obs[0]
    assert result.status == "success"
    assert result.replay_contract["model_calls_live"] == 1  # type: ignore[index]
    assert "unsupported_surface" in str(result.divergence_reason)


def test_s14_s16_served_surfaces_share_the_contract(agent: Agent) -> None:
    """Async, streamed and Responses API calls are counted, exhausted and
    reported exactly like sync chat calls (synthetic capsule, no capture)."""
    records = [
        openai_record("a", surface="openai.chat.completions"),
        openai_record("b", surface="openai.chat.completions"),
        openai_record("c", surface="openai.responses"),
    ]
    plan = [{"op": "async_chat"}, {"op": "stream_chat"}, {"op": "stream_responses"},
            {"op": "async_chat", "catch": True}]
    result, obs = agent.replay(records, plan)
    assert [o.get("content") or o.get("text") for o in obs[:3]] == ["a", "b", "c"]
    assert obs[3]["error"] == "ReplayQueueExhaustedError"
    assert result.status == "failure"
    assert (result.model_calls_mocked, result.model_calls_available) == (3, 3)
    assert result.model_calls_unmatched == 1
    assert _kinds(result) == ["model_queue_exhausted"]


def test_a_capsule_captured_before_adr_0304_keeps_refusing_async_and_streams(
    agent: Agent,
) -> None:
    """Unmarked records come from hooks that never recorded async or streamed
    calls; serving one would hand it a record that belonged to another call."""
    result, obs = agent.replay(
        [openai_record("sync answer")],
        [{"op": "async_chat", "catch": True}, {"op": "stream_chat", "catch": True},
         {"op": "chat"}],
    )
    assert [o.get("error") for o in obs[:2]] == ["ReplayUnsupportedSurfaceError"] * 2
    assert "captured before async and streamed calls were recorded" in obs[0]["message"]
    assert obs[2]["content"] == "sync answer"  # the sync call still gets its record
    assert result.model_calls_mocked == 1
    assert result.status == "failure"
    assert _kinds(result) == ["unsupported_surface"] * 2


# ── dispatcher reachability ──────────────────────────────────────────────────


def test_dispatcher_never_installed_is_named_in_the_divergence(agent: Agent) -> None:
    result, _ = agent.replay([openai_record("x")], [], command=["true"])
    assert result.status == "failure"
    assert "never installed" in str(result.divergence_reason)
    assert result.replay_contract["dispatcher_installed"] is False  # type: ignore[index]


def test_several_interpreters_consuming_the_queue_is_a_divergence(
    agent: Agent, tmp_path: Path
) -> None:
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import runpy, subprocess, sys\n"
        f"subprocess.run([sys.executable, {str(agent.path)!r}], check=True)\n"
        f"runpy.run_path({str(agent.path)!r}, run_name='__main__')\n"
    )
    result, _ = agent.replay(
        [openai_record("a"), openai_record("b")], [{"op": "chat"}],
        command=[sys.executable, str(parent)],
    )
    assert "multiple_interpreters" in _kinds(result)
    assert result.status == "failure"


def test_install_failure_stops_a_strict_replay_before_the_workload_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.replay import _dispatcher

    queue = tmp_path / "queue.json"
    queue.write_text("{not json")
    events = tmp_path / "events.jsonl"
    monkeypatch.setenv("NOVAFABRIC_REPLAY_QUEUE_PATH", str(queue))
    monkeypatch.setenv("NOVAFABRIC_REPLAY_EVENTS_PATH", str(events))
    monkeypatch.delenv("NOVAFABRIC_REPLAY_TOOL_QUEUE_PATH", raising=False)
    exits: list[int] = []
    monkeypatch.setattr(_dispatcher.os, "_exit", exits.append)

    monkeypatch.setenv("NOVAFABRIC_REPLAY_DIVERGENCE_POLICY", "fail")
    _dispatcher.install_from_env()
    assert exits == [_dispatcher.REPLAY_DISPATCHER_UNAVAILABLE_EXIT]

    monkeypatch.setenv("NOVAFABRIC_REPLAY_DIVERGENCE_POLICY", "warn")
    _dispatcher.install_from_env()
    assert exits == [_dispatcher.REPLAY_DISPATCHER_UNAVAILABLE_EXIT]  # warn does not exit

    lines = [json.loads(x) for x in events.read_text().splitlines()]
    assert [x["event"] for x in lines] == ["install_failed", "install_failed"]
    report = summarize([], [], lines, divergence_policy="fail", substitute_tools=True)
    assert report.divergences[0]["kind"] == "dispatcher_install_failed"


# ── in-process tool dispatcher: install path, recorded errors, restore ───────


def test_tool_dispatcher_install_serves_refuses_and_restores() -> None:
    import asyncio

    from mcp.client.session import ClientSession
    from mcp.shared.exceptions import McpError

    from novafabric.replay._dispatcher import MockToolDispatcher
    from novafabric.replay._errors import ReplayRecordedToolError, ReplayToolUnmatchedError

    rpc_error = mcp_record("boom", {}, "", tool_call_id="01HXAY7M5JZ8R7K4P9DPBYK2E1")
    rpc_error["status"] = "error"
    rpc_error["mcp"]["response_envelope"] = {
        "jsonrpc": "2.0", "id": "1", "error": {"code": -32000, "message": "server said no"},
    }
    hook_error = {
        **mcp_record("crash", {}, "", tool_call_id="01HXAY7M5JZ8R7K4P9DPBYK2E2"),
        "status": "error", "error": {"type": "TimeoutError", "message": "slow"},
    }
    hook_error["mcp"] = {"method": "tools/call", "response_envelope": None}
    original = ClientSession.call_tool
    dispatcher = MockToolDispatcher([mcp_record("ok", {"a": 1}, "fine"), rpc_error, hook_error])
    dispatcher.install()
    try:
        assert ClientSession.call_tool is not original
        session: Any = object()  # never touched for served or refused calls
        result = asyncio.run(ClientSession.call_tool(session, "ok", {"a": 1}))
        assert result.content[0].text == "fine"
        with pytest.raises(McpError, match="server said no"):
            asyncio.run(ClientSession.call_tool(session, "boom", {}))
        with pytest.raises(ReplayRecordedToolError, match="TimeoutError: slow"):
            asyncio.run(ClientSession.call_tool(session, "crash", {}))
        with pytest.raises(ReplayToolUnmatchedError):
            asyncio.run(ClientSession.call_tool(session, "ok", {"a": 1}))  # consumed
    finally:
        dispatcher.uninstall()
    assert ClientSession.call_tool is original


# ── contract helpers ─────────────────────────────────────────────────────────


def test_a_call_captured_by_both_the_hook_and_mcp_proxy_is_one_record() -> None:
    hook = mcp_record(
        "search", {"q": 1}, "x",
        started_at="2026-10-08T00:00:00.000000Z", finished_at="2026-10-08T00:00:02.000000Z",
    )
    proxy = {
        **mcp_record(
            "search", {"q": 1}, "x", tool_call_id="01HXAY7M5JZ8R7K4P9DPBYK2P1",
            started_at="2026-10-08T00:00:00.500000Z", finished_at="2026-10-08T00:00:01.500000Z",
        ),
        "extensions": {"io.novafabric.capture_method": "proxy"},
    }
    elicitation = {**mcp_record("search", {}, "x"), "mcp": {"method": "elicitation/create"}}
    assert interceptable_tool_calls([hook, proxy, elicitation]) == [hook]
    # a proxy record that is NOT nested in a hook record is a separate call
    assert len(interceptable_tool_calls([proxy])) == 1


def test_wire_duplicates_and_error_records_are_not_served() -> None:
    sdk = openai_record("ok")
    wire = {**openai_record("ok"), "gen_ai.response.choices": []}
    error = {**openai_record("ok"), "status": "error"}
    report = summarize([wire, sdk, error], [], [], divergence_policy="fail", substitute_tools=True)
    assert report.model_calls_recorded == 3
    assert report.model_calls_available == 1


# ── CLI surface ──────────────────────────────────────────────────────────────


def test_cli_help_documents_permissive() -> None:
    result = CliRunner().invoke(app, ["replay", "--help"])
    assert result.exit_code == 0
    assert_flag_in_help(result, "--permissive")


def test_cli_permissive_is_mocked_only(tmp_path: Path, agent: Agent) -> None:
    cap = write_capsule(tmp_path, agent.path, [])
    result = CliRunner().invoke(app, ["replay", str(cap), "--mode", "forensic", "--permissive"])
    assert result.exit_code == 1
    assert "--permissive only applies to --mode mocked" in result.output


def test_cli_prints_what_was_served_and_fails_on_divergence(
    tmp_path: Path, agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_PLAN", json.dumps([{"op": "chat"}]))
    cap = write_capsule(tmp_path, agent.path, [openai_record("a"), openai_record("b")])
    result = CliRunner().invoke(app, ["replay", str(cap), "-o", str(tmp_path / "r")])
    assert result.exit_code == 1
    assert "model calls: 1 of 2 served from the capsule" in result.output
    assert "divergence: model_calls_unconsumed" in result.output


# ── live network observer (ADR-0304) ─────────────────────────────────────────


class _Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, **fields: Any) -> None:
        self.items.append((event, fields))


def test_network_observer_reports_inet_connections_and_restores_socket() -> None:
    import socket

    from novafabric.replay._dispatcher import NetworkObserver

    original = socket.socket.connect
    events = _Events()
    observer = NetworkObserver(events, cap=1)  # type: ignore[arg-type]
    assert observer.install() is True
    server = socket.socket()
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(2)
        port = server.getsockname()[1]
        socket.create_connection(("127.0.0.1", port)).close()
        socket.create_connection(("127.0.0.1", port)).close()  # over the cap
        a, b = socket.socketpair()  # AF_UNIX: not network, never reported
        a.close()
        b.close()
    finally:
        server.close()
        observer.uninstall()
    assert events.items == [
        ("network_live", {"host": "127.0.0.1", "port": port}),
        ("network_live_capped", {"cap": 1}),
    ]
    assert socket.socket.connect is original


def test_network_observer_never_breaks_the_workload() -> None:
    import socket

    from novafabric.replay._dispatcher import NetworkObserver

    class Boom:
        def emit(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("log unavailable")

    observer = NetworkObserver(Boom())  # type: ignore[arg-type]
    sock = socket.socket()
    try:
        observer.seen(sock, ("10.0.0.1", 80))  # must not raise
        observer.seen(sock, "not-an-address")
    finally:
        sock.close()


def test_summary_counts_network_events_and_caps_destinations() -> None:
    events: list[dict[str, Any]] = [{"event": "network_observer_installed"}]
    events += [
        {"event": "network_live", "host": f"10.0.0.{i % 30}", "port": 443}
        for i in range(40)
    ]
    events.append({"event": "network_live_capped", "cap": 40})
    report = summarize([], [], events, divergence_policy="fail", substitute_tools=True)
    assert report.network_connections_live == 40
    out = report.as_dict()
    assert out["network_observed"] is True
    assert len(out["network_destinations"]) == 20
    assert out["network_connections_capped"] is True
    assert not report.diverged  # observation alone is never a divergence


# ── ADR-0306 slice 1: per-surface counters, the not-servable kind ───────────


def _python_record(
    name: str, arguments: dict[str, Any], value: Any, **ext: Any
) -> dict[str, Any]:
    return {
        "tool_call_id": "01HXAY7M5JZ8R7K4P9DPBYK2P0",
        "tool_name": name,
        "transport": "python",
        "arguments": arguments,
        "result": {"value": value},
        "status": "success",
        "extensions": {
            "io.novafabric.tool_surface": "python.function",
            "io.novafabric.result_codec": "json-v1",
            **ext,
        },
    }


def test_summary_counts_each_tool_surface_separately() -> None:
    from novafabric.replay._contract import TOOL_SURFACE_MCP, TOOL_SURFACE_PYTHON

    tools = [
        mcp_record("search", {"q": "a"}, "A"),
        _python_record("search", {"q": "a"}, {"hits": 1}),
        _python_record("lookup", {"id": 1}, None, **{
            "io.novafabric.result_codec": "not-servable",
            "io.novafabric.not_servable_reason": "the result is a tuple",
        }),
    ]
    events = [
        {"event": "tool_mocked", "tool_name": "search"},  # MCP events carry no surface
        {"event": "tool_mocked", "tool_name": "search", "surface": TOOL_SURFACE_PYTHON},
        {"event": "divergence", "kind": "tool_result_not_servable", "message": "m",
         "surface": TOOL_SURFACE_PYTHON, "consumed": True},
    ]
    report = summarize([], tools, events, divergence_policy="fail", substitute_tools=True)
    assert report.tool_calls_available == 2  # the not-servable record is not available
    assert report.tool_calls_mocked == 2
    # counted as a tool divergence, never a model one
    assert report.tool_calls_unmatched == 1 and report.model_calls_unmatched == 0
    assert report.tool_calls_unconsumed == 0  # the not-servable record was consumed
    by_surface = report.as_dict()["tool_calls_by_surface"]
    assert by_surface[TOOL_SURFACE_MCP] == {
        "recorded": 1, "available": 1, "mocked": 1, "live": 0, "refused": 0,
        "unmatched": 0, "unconsumed": 0,
    }
    assert by_surface[TOOL_SURFACE_PYTHON]["recorded"] == 2
    assert by_surface[TOOL_SURFACE_PYTHON]["available"] == 1
    assert by_surface[TOOL_SURFACE_PYTHON]["unmatched"] == 1
    assert TOOL_SURFACE_PYTHON in report.interception_surfaces


def test_unconsumed_message_names_the_surface_of_each_leftover() -> None:
    from novafabric.replay._contract import TOOL_SURFACE_MCP, TOOL_SURFACE_PYTHON

    only_python = summarize(
        [], [_python_record("f", {}, 1)], [], divergence_policy="fail", substitute_tools=True
    )
    (div,) = only_python.divergences
    assert div["kind"] == "tool_calls_unconsumed"
    assert div["message"] == (
        f"1 of 1 recorded {TOOL_SURFACE_PYTHON} results were never requested by the replay"
    )
    both = summarize(
        [], [mcp_record("g", {}, "x"), _python_record("f", {}, 1)], [],
        divergence_policy="fail", substitute_tools=True,
    )
    (div,) = both.divergences
    assert f"{TOOL_SURFACE_MCP}: 1 of 1" in div["message"]
    assert f"{TOOL_SURFACE_PYTHON}: 1 of 1" in div["message"]
    assert div["unconsumed"] == {TOOL_SURFACE_MCP: 1, TOOL_SURFACE_PYTHON: 1}


def test_refused_and_live_python_calls_are_counted() -> None:
    from novafabric.replay._contract import TOOL_SURFACE_PYTHON

    events = [
        {"event": "tool_live", "tool_name": "f", "surface": TOOL_SURFACE_PYTHON},
        {"event": "tool_refused", "tool_name": "g", "surface": TOOL_SURFACE_PYTHON},
    ]
    report = summarize([], [], events, divergence_policy="warn", substitute_tools=True)
    assert (report.tool_calls_live, report.tool_calls_refused) == (1, 1)
    assert report.as_dict()["tool_calls_refused"] == 1


def test_the_tool_matcher_index_keeps_one_to_one_order() -> None:
    records = [
        mcp_record("t", {"a": 1}, "first", tool_call_id="01HXAY7M5JZ8R7K4P9DPBYK2T1"),
        mcp_record("t", {"a": 1}, "second", tool_call_id="01HXAY7M5JZ8R7K4P9DPBYK2T2"),
        mcp_record("t", {"a": 2}, "other", tool_call_id="01HXAY7M5JZ8R7K4P9DPBYK2T3"),
    ]
    matcher = ToolCallMatcher(records)
    # an id-tier match consumes a record the signature queue still lists
    assert matcher.match("01HXAY7M5JZ8R7K4P9DPBYK2T1", "t", {"a": 1}).how == "id"
    second = matcher.match(None, "t", {"a": 1})
    assert second.record is records[1]
    exhausted = matcher.match(None, "t", {"a": 1})
    assert exhausted.record is None
    assert exhausted.reason == "every recorded call with these arguments was already consumed"
    assert matcher.match(None, "t", {"a": 3}).reason == (
        "the tool was recorded, but not with these arguments"
    )
    assert matcher.match(None, "u", {}).reason == "no recorded call to this tool"
    assert matcher.consumed_count == 2
    assert matcher.unconsumed() == [records[2]]
