"""``_tool_call_outcomes.collapse_tool_divergences``: a tool divergence followed by
that call's marked ``tool_live`` / ``tool_refused`` is one call (c08 re-replay,
2026-11-21: "4 recorded, 8 replayed"). The e2e cases per surface live in the
LangChain, ADK, OpenAI Agents, ``record.tool`` and MCP replay tests."""

from __future__ import annotations

from typing import Any

from novafabric.replay._fidelity import tool_sequences
from novafabric.replay._replay_capsule import PLACEHOLDER_EXT, replayed_streams
from novafabric.replay._tool_call_outcomes import AFTER_DIVERGENCE, collapse_tool_divergences

LC = "langchain_core.tools.BaseTool"


def _div(name: str, kind: str = "tool_result_not_servable", pid: int = 1) -> dict[str, Any]:
    return {"event": "divergence", "kind": kind, "tool_name": name, "surface": LC, "pid": pid}


def _out(name: str, kind: str = "tool_live", pid: int = 1, **extra: Any) -> dict[str, Any]:
    return {"event": kind, "tool_name": name, "surface": LC, "pid": pid,
            AFTER_DIVERGENCE: True, **extra}


def test_a_divergence_then_its_live_run_is_one_entry_at_the_divergence_place() -> None:
    model = {"event": "model_served", "queue": "openai", "call_index": 0}
    events = [_div("ls"), model, _out("ls")]
    assert collapse_tool_divergences(events) == [_out("ls"), model]


def test_parallel_calls_of_one_tool_each_pair_with_one_divergence() -> None:
    events = [_div("read"), _div("read"), _out("read"), _out("read", "tool_refused")]
    assert [e["event"] for e in collapse_tool_divergences(events)] == [
        "tool_live", "tool_refused",
    ]


def test_unmarked_unpaired_or_other_process_outcomes_are_never_dropped() -> None:
    unmarked = {"event": "tool_live", "tool_name": "ls", "surface": LC, "pid": 1,
                "override": "allow"}
    other_pid = _out("ls", pid=2)
    other_tool = _out("cat")
    events = [_div("ls"), unmarked, other_pid, other_tool]
    assert collapse_tool_divergences(events) == events


def test_a_strict_divergence_with_no_outcome_stays_unmatched() -> None:
    events = [_div("ls", "tool_call_unmatched")]
    assert collapse_tool_divergences(events) == events
    assert tool_sequences([], events)[1] == [f"{LC}:ls:unmatched"]


def test_sequence_and_replay_capsule_count_one_per_call_and_input_is_not_mutated() -> None:
    events = [_div("ls"), _out("ls"), _div("cat"), _out("cat", "tool_refused")]
    snapshot = [dict(e) for e in events]
    assert tool_sequences([], events)[1] == [f"{LC}:ls:live", f"{LC}:cat:refused"]
    _models, tools = replayed_streams([], [], events)
    assert [t["extensions"][PLACEHOLDER_EXT] for t in tools] == ["live", "refused"]
    assert events == snapshot
