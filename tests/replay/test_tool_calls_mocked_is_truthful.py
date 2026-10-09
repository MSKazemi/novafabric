"""ADR-0261 / ADR-0300 — `tool_calls_mocked` must not assert work the engine never did.

ADR-0261: every replay path used to report `tool_calls_mocked=len(tool_calls)`,
which reads as "this many tool responses were served from cache" when none ever
were. The count was set to 0 and the capsule's count moved to
`tool_calls_available`, with a pinned premise: no tool dispatcher is installed.

ADR-0300 changed the premise deliberately: `mocked` mode now installs
`MockToolDispatcher` on `mcp.ClientSession.call_tool`. These tests pin the new
contract against the engine source:

* only `mocked` mode installs a tool dispatcher, and its `tool_calls_mocked`
  comes from the dispatcher's event log (`_contract.summarize`), never from a
  length;
* every other path still reports 0;
* the capsule's full tool-call count is preserved (`tool_calls_recorded`), and
  `tool_calls_available` now means "on a surface a dispatcher can serve".

Behavioural coverage lives in `tests/replay/test_mocked_replay_contract.py`.
"""

from __future__ import annotations

import inspect
import re

from novafabric.replay import _dispatcher, _engine
from novafabric.replay._result import ReplayResult


def _result(**kw: object) -> ReplayResult:
    base: dict[str, object] = {
        "replay_id": "r1", "replay_of_run_id": "run1", "mode": "mocked",
        "status": "success", "start_time": "2026-01-01T00:00:00Z",
        "end_time": "2026-01-01T00:00:01Z", "duration_ms": 1000,
        "policy_flags_used": [], "env_warnings": [],
    }
    base.update(kw)
    return ReplayResult(**base)  # type: ignore[arg-type]


def test_tool_dispatcher_is_installed_only_by_the_mocked_path() -> None:
    """The ADR-0300 premise: a real install path exists and only `_mocked` uses it."""
    assert hasattr(_dispatcher.MockToolDispatcher, "install")
    src = inspect.getsource(_engine.ReplayEngine._mocked)
    assert "_run_replay_subprocess(" in src and "tool_calls" in src
    intervention = inspect.getsource(_engine.ReplayEngine._intervention)
    # intervention passes no tool queue -> no tool dispatcher is installed
    assert re.search(r"_run_mocked_subprocess\(\s*command, mutated_model_calls\s*\)", intervention)


def test_engine_never_reports_a_nonzero_mocked_tool_count_from_a_length() -> None:
    """No keyword assignment sets the field from anything but 0; the mocked path
    takes it from the dispatcher's event log via `_contract_fields`."""
    src = inspect.getsource(_engine)
    assignments = re.findall(r"tool_calls_mocked\s*=\s*([^,\n]+)", src)
    assert assignments, "field vanished -- update this test deliberately"
    for value in assignments:
        assert value.strip() == "0", (
            f"tool_calls_mocked={value.strip()!r} claims substitutions that the "
            "engine does not perform"
        )
    assert '"tool_calls_mocked": report.tool_calls_mocked' in src


def test_capsule_tool_call_count_is_still_reported() -> None:
    """The information was preserved, not deleted, on every replay path."""
    src = inspect.getsource(_engine)
    # forensic, dry-run, intervention, the mocked CapsuleNotReplayable refusal,
    # and the mocked ToolOverrideUnenforceable refusal (ADR-0306 Q5)
    assert src.count("tool_calls_recorded=len(") == 5
    assert '"tool_calls_recorded": report.tool_calls_recorded' in src  # mocked


def test_available_is_optional_and_omitted_when_unset() -> None:
    d = _result().as_dict()
    assert "tool_calls_available" not in d
    assert "tool_calls_recorded" not in d


def test_available_is_serialised_when_set() -> None:
    d = _result(tool_calls_available=7, tool_calls_recorded=9).as_dict()
    assert d["tool_calls_available"] == 7
    assert d["tool_calls_recorded"] == 9
    assert d["tool_calls_mocked"] == 0
