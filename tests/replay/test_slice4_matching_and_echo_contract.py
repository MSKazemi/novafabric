"""ADR-0306 slice 4, contract level: redact-then-hash for MCP (D12.3) with the
dual-hash fallback, the D10 echo check per provider shape, and nested-coverage
accounting.

The end-to-end proofs are in ``test_echo_and_nested_coverage_e2e.py`` and
``test_python_tool_replay_e2e.py``; this file pins the rules one by one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from _mocked_replay_agent import mcp_record, openai_record, write_agent, write_capsule

from novafabric.capture.secrets import SecretScannerV0, redact_json_strings
from novafabric.replay import ReplayEngine, ReplayFlags
from novafabric.replay._contract import (
    TOOL_SURFACE_MCP,
    TOOL_SURFACE_PYTHON,
    ReplayEventLog,
    ToolCallMatcher,
    nested_boundary_ids,
    normalized_arg_hash,
    read_events,
    summarize,
    tool_result_messages,
)
from novafabric.replay._dispatcher import MockModelDispatcher

#: Two different secrets one ADR-0009 rule (``github-token``) masks to one value.
TOKEN_A = "ghp" + "_" + "Ab3dEf6hIj" * 3 + "Kl3mNp"
TOKEN_B = "ghp" + "_" + "Zy9xWv8uTs" * 3 + "Rq7pOn"
_ID1, _ID2 = "01HXAY7M5JZ8R7K4P9DPBYK2T1", "01HXAY7M5JZ8R7K4P9DPBYK2T2"


def _redacted(value: Any) -> Any:
    return redact_json_strings(value)


# ── D12.3: redact-then-hash for MCP, with the raw exact tier first ───────────


def test_redaction_masks_both_tokens_to_the_same_value() -> None:
    """Non-vacuity for the tests below: the two tokens really collapse."""
    a, b = _redacted({"t": TOKEN_A}), _redacted({"t": TOKEN_B})
    assert a == b and TOKEN_A not in json.dumps(a)


def test_a_sealed_mcp_record_matches_the_live_raw_secret() -> None:
    sealed = mcp_record("login", _redacted({"token": TOKEN_A}), "ok")
    match = ToolCallMatcher([sealed]).match(None, "login", {"token": TOKEN_A})
    assert match.record is sealed
    assert match.how == "redacted-signature"  # the match relied on redaction


def test_an_unsealed_capsule_matches_exactly_as_before_redact_then_hash() -> None:
    """The dual-hash fallback: raw-equal arguments always win, so recorded order
    among records whose secrets redact alike never picks the wrong one."""
    first = mcp_record("login", {"token": TOKEN_A}, "for-A", tool_call_id=_ID1)
    second = mcp_record("login", {"token": TOKEN_B}, "for-B", tool_call_id=_ID2)
    matcher = ToolCallMatcher([first, second])
    got_b = matcher.match(None, "login", {"token": TOKEN_B})
    assert got_b.record is second and got_b.how == "signature"
    got_a = matcher.match(None, "login", {"token": TOKEN_A})
    assert got_a.record is first and got_a.how == "signature"


def test_without_a_raw_match_recorded_order_decides_among_collapsed_secrets() -> None:
    first = mcp_record("login", _redacted({"token": TOKEN_A}), "1", tool_call_id=_ID1)
    second = mcp_record("login", _redacted({"token": TOKEN_B}), "2", tool_call_id=_ID2)
    matcher = ToolCallMatcher([first, second])
    assert matcher.match(None, "login", {"token": TOKEN_B}).record is first
    assert matcher.match(None, "login", {"token": TOKEN_A}).record is second
    assert matcher.match(None, "login", {"token": TOKEN_A}).record is None


def test_arguments_without_a_secret_match_by_plain_signature() -> None:
    rec = mcp_record("search", {"q": "a"}, "A")
    match = ToolCallMatcher([rec]).match(None, "search", {"q": "a"})
    assert match.record is rec and match.how == "signature"


class _McpReplay:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.agent = write_agent(root)
        self.out = root / "out.json"
        self.side = root / "side.log"
        self.mp = monkeypatch
        monkeypatch.setenv("AGENT_OUT", str(self.out))
        monkeypatch.setenv("AGENT_SIDE", str(self.side))
        for var in ("AGENT_CANNED", "FAKE_ANTHROPIC_CANNED", "FORCE_COLOR", "COLORTERM"):
            monkeypatch.delenv(var, raising=False)

    def run(
        self, tool_calls: list[dict[str, Any]], plan: list[dict[str, Any]]
    ) -> tuple[Any, list[dict[str, Any]], Path]:
        self.mp.setenv("AGENT_PLAN", json.dumps(plan))
        cap = write_capsule(self.root, self.agent, [], tool_calls)
        SecretScannerV0(cap, "run-under-test").scan_and_redact()  # seal-time redaction
        base = self.root / "replays"
        result = ReplayEngine(
            capsule_dir=cap, flags=ReplayFlags(mode="mocked"), base_dir=base
        ).run()
        observed = json.loads(self.out.read_text()) if self.out.exists() else []
        return result, observed, base / result.replay_id


@pytest.fixture
def mcp_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _McpReplay:
    pytest.importorskip("mcp")
    pytest.importorskip("openai")
    return _McpReplay(tmp_path, monkeypatch)


def test_an_mcp_call_whose_argument_the_scanner_redacted_is_served(
    mcp_replay: _McpReplay,
) -> None:
    """Red before slice 4: the sealed record held ``[REDACTED:github-token]`` and
    the replayed call the raw token, so the raw hashes differed and strict replay
    refused the call."""
    rec = mcp_record("get_weather", {"city": TOKEN_A}, "recorded-weather")
    result, obs, _ = mcp_replay.run(
        [rec], [{"op": "tool", "name": "get_weather", "args": {"city": TOKEN_A}}]
    )
    capsule_text = (mcp_replay.root / "capsule" / "tool-calls.jsonl").read_text()
    assert TOKEN_A not in capsule_text and "[REDACTED:" in capsule_text
    assert obs[0]["text"] == "recorded-weather"
    assert not mcp_replay.side.exists(), "the live MCP tool ran"
    assert result.status == "success", result.divergence_reason
    assert result.tool_calls_mocked == 1


def test_an_unmatched_mcp_call_reports_only_a_redacted_digest(mcp_replay: _McpReplay) -> None:
    rec = mcp_record("get_weather", {"city": "Paris"}, "x")
    live = {"city": TOKEN_B}
    result, obs, replay_dir = mcp_replay.run(
        [rec], [{"op": "tool", "name": "get_weather", "args": live, "catch": True}]
    )
    assert obs[0]["error"] == "ReplayToolUnmatchedError"
    assert result.status == "failure"
    (div,) = [d for d in result.replay_contract["divergences"]  # type: ignore[index]
              if d["kind"] == "tool_call_unmatched"]
    assert div["arguments_hash"] == normalized_arg_hash(_redacted(live))
    raw = normalized_arg_hash(live)
    for path in replay_dir.rglob("*"):
        if path.is_file():
            text = path.read_text(errors="replace")
            assert raw not in text and TOKEN_B not in text, path


# ── D10: what the echo check reads, per provider shape ───────────────────────


def test_tool_results_are_read_from_every_served_request_shape() -> None:
    chat = [
        {"role": "user", "content": "q"},
        {"role": "tool", "tool_call_id": "call_1", "content": "22C"},
    ]
    anthropic = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "22C"},
        {"type": "text", "text": "and?"},
    ]}]
    responses = [{"type": "function_call_output", "call_id": "fc_1", "output": "22C"}]
    assert tool_result_messages(chat) == {"call_1": "22C"}
    assert tool_result_messages(anthropic) == {
        "toolu_1": {"content": "22C", "is_error": False}
    }
    assert tool_result_messages(responses) == {"fc_1": "22C"}
    # Nothing recorded is never "no tool results": the check must say not checked.
    assert tool_result_messages([]) is None
    assert tool_result_messages(None) is None
    assert tool_result_messages([{"role": "user", "content": "q"}]) == {}


def _anthropic_record(messages: list[dict[str, Any]] | None) -> dict[str, Any]:
    record = openai_record("ok", system="anthropic")
    if messages is not None:
        record["gen_ai.request.messages"] = messages
    return record


def _block(content: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": content,
         "is_error": is_error},
    ]}


@pytest.mark.parametrize(
    ("recorded", "live", "outcome"),
    [
        ([_block("22C")], [_block("22C")], "matched"),
        ([_block("22C")], [_block("30C")], "mismatched"),
        ([_block("22C")], [_block("22C", is_error=True)], "mismatched"),
        (None, [_block("22C")], "not_checked"),
    ],
    ids=["same", "different-content", "now-an-error", "not-recorded"],
)
def test_the_echo_check_compares_anthropic_tool_results(
    tmp_path: Path, recorded: Any, live: Any, outcome: str
) -> None:
    log = tmp_path / "events.jsonl"
    dispatcher = MockModelDispatcher(
        [_anthropic_record(recorded)], events=ReplayEventLog(log)
    )
    dispatcher._take("anthropic", request={"messages": live})
    (echo,) = [e for e in read_events(log) if e["event"] == "tool_result_echo"]
    assert echo["outcome"] == outcome
    assert echo["tool_call_id"] == "toolu_1"
    assert "22C" not in log.read_text() and "30C" not in log.read_text()


def test_each_tool_result_is_checked_once_and_a_failure_never_breaks_serving(
    tmp_path: Path,
) -> None:
    log = tmp_path / "events.jsonl"
    msgs = [{"role": "tool", "tool_call_id": "call_1", "content": "x"}]
    records = [
        {**openai_record("a"), "gen_ai.request.messages": msgs},
        {**openai_record("b", call_id="01HXAY7M5JZ8R7K4P9DPBYK2M1"),
         "gen_ai.request.messages": msgs},
    ]
    dispatcher = MockModelDispatcher(records, events=ReplayEventLog(log))
    dispatcher._take("openai", request={"messages": msgs})
    dispatcher._take("openai", request={"messages": msgs})  # the same history again
    # An unreadable request is skipped, never raised into the workload.
    dispatcher._echo_seen.clear()
    first, _ = MockModelDispatcher(records, events=ReplayEventLog(log))._take(
        "openai", request={"messages": object()}
    )
    assert first["model_call_id"] == records[0]["model_call_id"]
    echoes = [e for e in read_events(log) if e["event"] == "tool_result_echo"]
    assert [e["outcome"] for e in echoes] == ["matched"]


def test_echo_counts_are_summarised_and_never_become_divergences() -> None:
    events = [
        {"event": "tool_result_echo", "outcome": "matched", "tool_call_id": "a"},
        {"event": "tool_result_echo", "outcome": "mismatched", "tool_call_id": "b",
         "surface": "openai.chat.completions.create", "call_index": 2,
         "reason": "the result the replay sent back differs from the recorded one"},
        {"event": "tool_result_echo", "outcome": "not_checked", "tool_call_id": "c",
         "reason": "no tool result with this id was recorded at this position"},
    ]
    report = summarize([], [], events, divergence_policy="fail", substitute_tools=True)
    assert report.divergences == [] and not report.diverged
    echo = report.as_dict()["tool_result_echo"]
    assert (echo["matched"], echo["mismatched"], echo["not_checked"]) == (1, 1, 1)
    (mismatch,) = echo["mismatches"]
    assert mismatch["kind"] == "tool_result_echo_mismatch"
    assert "call #3" in mismatch["message"]


# ── nested coverage: accounting ──────────────────────────────────────────────


def _within(record: dict[str, Any], boundary: str) -> dict[str, Any]:
    record.setdefault("extensions", {})["io.novafabric.within_tool_call_id"] = boundary
    return record


def test_covered_records_are_neither_served_nor_left_over() -> None:
    model_calls = [
        openai_record("top"),
        _within(openai_record("nested", call_id="01HXAY7M5JZ8R7K4P9DPBYK2M1"), "B1"),
    ]
    tools = [_within(mcp_record("inner", {}, "x"), "B1")]
    events = [
        {"event": "model_served", "queue": "openai"},
        {"event": "model_covered", "queue": "openai"},
        {"event": "tool_covered", "surface": TOOL_SURFACE_MCP},
    ]
    report = summarize(model_calls, tools, events, divergence_policy="fail",
                       substitute_tools=True)
    assert report.model_calls_unconsumed == 0 and report.tool_calls_unconsumed == 0
    assert (report.model_calls_covered, report.tool_calls_covered) == (1, 1)
    assert report.queues_fully_consumed and not report.diverged
    by_surface = report.as_dict()["tool_calls_by_surface"]
    assert by_surface[TOOL_SURFACE_MCP]["covered"] == 1
    assert by_surface[TOOL_SURFACE_PYTHON]["covered"] == 0


def test_the_model_queue_skips_covered_records_in_place(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    records = [
        openai_record("before", call_id="01HXAY7M5JZ8R7K4P9DPBYK2M0"),
        _within(openai_record("nested", call_id="01HXAY7M5JZ8R7K4P9DPBYK2M1"), "B1"),
        openai_record("after", call_id="01HXAY7M5JZ8R7K4P9DPBYK2M2"),
    ]
    dispatcher = MockModelDispatcher(records, events=ReplayEventLog(log))
    assert dispatcher.cover_nested({"B1"}) == 1
    assert dispatcher.cover_nested({"B1"}) == 0  # covered once
    served = [dispatcher._take("openai")[0]["model_call_id"] for _ in range(2)]
    assert served == ["01HXAY7M5JZ8R7K4P9DPBYK2M0", "01HXAY7M5JZ8R7K4P9DPBYK2M2"]
    kinds = [e["event"] for e in read_events(log)]
    assert kinds.count("model_covered") == 1
    assert "divergence" not in kinds  # the global order skipped the covered position


def test_nested_boundaries_are_collected_transitively() -> None:
    tools = [
        {"tool_call_id": "inner", "extensions": {"io.novafabric.within_tool_call_id": "mid"}},
        {"tool_call_id": "mid", "extensions": {"io.novafabric.within_tool_call_id": "top"}},
        {"tool_call_id": "other", "extensions": {"io.novafabric.within_tool_call_id": "x"}},
    ]
    assert nested_boundary_ids("top", tools) == {"top", "mid", "inner"}
