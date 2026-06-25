from __future__ import annotations

from novafabric.replay._flags import ReplayFlags
from novafabric.replay._policy import PolicyEvaluator


def _tc(tool_name: str = "send_email", mutation_class: str = "non-idempotent-write") -> dict:
    return {
        "tool_call_id": "01ABC",
        "tool_name": tool_name,
        "mutation_class": mutation_class,
    }


def test_mocked_mode_always_mocks() -> None:
    flags = ReplayFlags(mode="mocked", allow_mutating=True)
    ev = PolicyEvaluator({}, flags)
    decision = ev.check_tool(_tc("db_write", "non-idempotent-write"))
    assert decision.decision == "mock"


def test_forensic_mode_always_mocks() -> None:
    flags = ReplayFlags(mode="forensic")
    ev = PolicyEvaluator({}, flags)
    decision = ev.check_tool(_tc("read_file", "read-only"))
    assert decision.decision == "mock"
    assert "forensic" in decision.reason


def test_tool_override_takes_precedence() -> None:
    policy = {
        "tool_overrides": [
            {"tool_name": "safe_lookup", "action": "replay"},
        ]
    }
    flags = ReplayFlags(mode="mocked")
    ev = PolicyEvaluator(policy, flags)
    decision = ev.check_tool({
        "tool_call_id": "XY",
        "tool_name": "safe_lookup",
        "mutation_class": "read-only",
    })
    assert decision.decision == "allow"
    assert "tool_override" in decision.reason


def test_check_all_returns_one_per_call() -> None:
    flags = ReplayFlags(mode="mocked")
    ev = PolicyEvaluator({}, flags)
    tcs = [_tc("a"), _tc("b"), _tc("c")]
    decisions = ev.check_all(tcs)
    assert len(decisions) == 3


def test_dry_run_report_lists_tools() -> None:
    flags = ReplayFlags(mode="mocked", dry_run=True)
    ev = PolicyEvaluator({}, flags)
    tcs = [
        {"tool_call_id": "1", "tool_name": "get_weather", "mutation_class": "read-only"},
        {"tool_call_id": "2", "tool_name": "post_tweet", "mutation_class": "external-side-effect"},
    ]
    report = ev.dry_run_report(tcs)
    assert "get_weather" in report
    assert "post_tweet" in report
    assert "[dry-run" in report


def test_dry_run_report_empty_capsule() -> None:
    flags = ReplayFlags(mode="mocked")
    ev = PolicyEvaluator({}, flags)
    report = ev.dry_run_report([])
    assert "No tool calls" in report
