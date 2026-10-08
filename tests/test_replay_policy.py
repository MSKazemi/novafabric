from __future__ import annotations

from novafabric.replay._flags import ReplayFlags
from novafabric.replay._policy import PolicyEvaluator


def _tc(tool_name: str = "send_email", mutation_class: str = "non-idempotent-write") -> dict:
    return {
        "tool_call_id": "01ABC",
        "tool_name": tool_name,
        "mutation_class": mutation_class,
    }


def test_mocked_mode_mocks_only_the_intercepted_surface() -> None:
    """ADR-0300: an MCP tools/call is served from the capsule; any other
    transport is not intercepted and the dry run must say it runs live (it used
    to claim "all tools served from cache" for every call)."""
    flags = ReplayFlags(mode="mocked", allow_mutating=True)
    ev = PolicyEvaluator({}, flags)
    mcp_call = {**_tc("db_write", "non-idempotent-write"), "transport": "mcp"}
    decision = ev.check_tool(mcp_call)
    assert decision.decision == "mock"
    assert "mcp.ClientSession.call_tool" in decision.reason


def test_mocked_mode_reports_non_intercepted_tools_as_live() -> None:
    flags = ReplayFlags(mode="mocked")
    ev = PolicyEvaluator({}, flags)
    decision = ev.check_tool({**_tc("db_write"), "transport": "http"})
    assert decision.decision == "live"
    assert "runs live" in decision.reason
    assert "[LIVE]" in ev.dry_run_report([{**_tc("db_write"), "transport": "http"}])


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


def _schema_valid(policy: dict) -> None:
    """The override must be valid against both packaged replay-policy schemas."""
    import json
    from pathlib import Path

    import jsonschema

    root = Path(__file__).resolve().parents[1]
    for path in (
        root / "schemas" / "replay-policy.schema.json",
        root / "src" / "novafabric" / "schemas" / "replay-policy.schema.json",
    ):
        schema = json.loads(path.read_text())
        override_schema = {**schema["$defs"]["ToolOverride"], "$defs": schema["$defs"]}
        for override in policy["tool_overrides"]:
            jsonschema.validate(override, override_schema)


def test_schema_shape_allow_true_re_executes_the_tool() -> None:
    """The schema defines `{tool_name, allow: bool}`; the evaluator used to read only
    `action`, so a schema-valid override was silently ignored, even by --dry-run."""
    policy = {"tool_overrides": [{"tool_name": "safe_lookup", "allow": True}]}
    _schema_valid(policy)
    ev = PolicyEvaluator(policy, ReplayFlags(mode="mocked"))
    decision = ev.check_tool({**_tc("safe_lookup", "read-only"), "transport": "mcp"})
    assert decision.decision == "allow"
    assert decision.reason == "tool_override: allow=true"


def test_schema_shape_allow_false_refuses_the_tool() -> None:
    policy = {"tool_overrides": [{"tool_name": "send_email", "allow": False}]}
    _schema_valid(policy)
    ev = PolicyEvaluator(policy, ReplayFlags(mode="mocked"))
    decision = ev.check_tool({**_tc("send_email"), "transport": "http"})
    assert decision.decision == "deny"
    assert decision.reason == "tool_override: allow=false"
    assert "send_email" in ev.dry_run_report([{**_tc("send_email"), "transport": "http"}])


def test_override_with_neither_allow_nor_action_is_not_applied() -> None:
    """A malformed override is ignored rather than guessed at: the mode's own rule decides."""
    policy = {"tool_overrides": [{"tool_name": "db_write"}]}
    ev = PolicyEvaluator(policy, ReplayFlags(mode="mocked"))
    decision = ev.check_tool({**_tc("db_write"), "transport": "http"})
    assert decision.decision == "live"
    assert "tool_override" not in decision.reason
