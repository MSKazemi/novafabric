"""ADR-0261 — `tool_calls_mocked` must not assert work the engine never did.

Every replay path used to report `tool_calls_mocked=len(tool_calls)`, which
reads as "this many tool responses were served from cache". None ever were:
`_run_mocked_subprocess` writes only model calls into the replay queue, and the
subprocess hook loader instantiates only `MockModelDispatcher`.
`MockToolDispatcher` exists but exposes no `install()` and is referenced nowhere
outside its own module.

These tests pin the two halves of the correction: the count is truthful, and the
capsule's actual tool-call count is still available under a name that does not
claim substitution. They are written against the source of truth rather than a
mock, so they fail the moment a dispatcher IS wired up -- at which point the
engine should set a real number and this file should be updated deliberately.
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


def test_mock_tool_dispatcher_is_still_not_installed_anywhere() -> None:
    """The premise of the correction. If this fails, revisit the engine."""
    assert not hasattr(_dispatcher.MockToolDispatcher, "install")
    engine_src = inspect.getsource(_engine)
    assert "MockToolDispatcher" not in engine_src


def test_engine_never_reports_a_nonzero_mocked_tool_count() -> None:
    """No assignment in the engine sets the field from a length."""
    src = inspect.getsource(_engine)
    assignments = re.findall(r"tool_calls_mocked\s*=\s*([^,\n]+)", src)
    assert assignments, "field vanished -- update this test deliberately"
    for value in assignments:
        assert value.strip() == "0", (
            f"tool_calls_mocked={value.strip()!r} claims substitutions that the "
            "engine does not perform"
        )


def test_capsule_tool_call_count_is_still_reported() -> None:
    """The information was preserved, not deleted."""
    src = inspect.getsource(_engine)
    assert src.count("tool_calls_available=") == 4, (
        "every replay path should still report the capsule's tool-call count"
    )


def test_available_is_optional_and_omitted_when_unset() -> None:
    assert "tool_calls_available" not in _result().as_dict()


def test_available_is_serialised_when_set() -> None:
    d = _result(tool_calls_available=7).as_dict()
    assert d["tool_calls_available"] == 7
    assert d["tool_calls_mocked"] == 0
