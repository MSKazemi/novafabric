from __future__ import annotations

import pytest

from novafabric.replay._flags import ReplayFlags
from novafabric.replay._policy import PolicyEvaluator, decide_intercepted, gating_mutation_class


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
    """Outside mocked mode no tool dispatcher exists; the legacy table is kept
    (the legacy `action: replay` shape is still read)."""
    policy = {
        "tool_overrides": [
            {"tool_name": "safe_lookup", "action": "replay"},
        ]
    }
    flags = ReplayFlags(mode="semantic")
    ev = PolicyEvaluator(policy, flags)
    decision = ev.check_tool({
        "tool_call_id": "XY",
        "tool_name": "safe_lookup",
        "mutation_class": "read-only",
    })
    assert decision.decision == "allow"
    assert "tool_override" in decision.reason


def test_legacy_action_shape_is_enforced_like_allow() -> None:
    """`action: replay|refuse` maps to `allow: true|false` in the table the
    replayed process enforces; another action is ignored, never guessed at."""
    policy = {"tool_overrides": [
        {"tool_name": "a", "action": "replay"},
        {"tool_name": "b", "action": "refuse"},
        {"tool_name": "c", "action": "mock"},
    ]}
    table = PolicyEvaluator(policy, ReplayFlags()).tool_policy_table()
    assert table == {"overrides": {"a": {"allow": True}, "b": {"allow": False}}}


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


def test_schema_shape_allow_true_re_executes_the_tool_only_with_the_ladder_flag() -> None:
    """The schema defines `{tool_name, allow: bool}`. ADR-0306 Q4 (owner decision
    2026-10-09): a permission read from the capsule takes effect only with the
    operator's ladder flag -- for MCP always --allow-unknown-mutation, whatever
    class the capsule recorded."""
    policy = {"tool_overrides": [{"tool_name": "safe_lookup", "allow": True}]}
    _schema_valid(policy)
    call = {**_tc("safe_lookup", "read-only"), "transport": "mcp"}
    ev = PolicyEvaluator(policy, ReplayFlags(mode="mocked", allow_readonly=True))
    decision = ev.check_tool(call)
    assert decision.decision == "mock"
    assert "override not honoured: needs --allow-unknown-mutation" in ev.dry_run_report([call])
    ev = PolicyEvaluator(policy, ReplayFlags(mode="mocked", allow_unknown_mutation=True))
    decision = ev.check_tool(call)
    assert decision.decision == "live"
    assert "[LIVE (override honoured)]" in ev.dry_run_report([call])


def test_schema_shape_allow_false_on_an_intercepted_tool_is_never_live() -> None:
    policy = {"tool_overrides": [{"tool_name": "send_email", "allow": False}]}
    _schema_valid(policy)
    call = {**_tc("send_email"), "transport": "mcp"}
    for flags in (
        ReplayFlags(mode="mocked"),
        ReplayFlags(mode="mocked", permissive=True, allow_unknown_mutation=True),
    ):
        ev = PolicyEvaluator(policy, flags)
        assert ev.check_tool(call).decision == "mock"
        assert "[MOCK (never live)] send_email" in ev.dry_run_report([call])


def test_schema_shape_allow_false_on_a_non_intercepted_tool_refuses_to_start() -> None:
    """ADR-0306 Q5: strict replay refuses to start; --permissive runs it live."""
    policy = {"tool_overrides": [{"tool_name": "send_email", "allow": False}]}
    _schema_valid(policy)
    call = {**_tc("send_email"), "transport": "http"}
    ev = PolicyEvaluator(policy, ReplayFlags(mode="mocked"))
    decision = ev.check_tool(call)
    assert decision.decision == "deny"
    assert "refuses to start" in decision.reason
    assert ev.unenforceable_overrides([call]) == [
        {"tool_name": "send_email", "transports": ["http"]}
    ]
    ev = PolicyEvaluator(policy, ReplayFlags(mode="mocked", permissive=True))
    assert ev.check_tool(call).decision == "live"
    assert "[LIVE (override cannot be enforced)]" in ev.dry_run_report([call])


def test_override_with_neither_allow_nor_action_is_not_applied() -> None:
    """A malformed override is ignored rather than guessed at: the mode's own rule decides."""
    policy = {"tool_overrides": [{"tool_name": "db_write"}]}
    ev = PolicyEvaluator(policy, ReplayFlags(mode="mocked"))
    decision = ev.check_tool({**_tc("db_write"), "transport": "http"})
    assert decision.decision == "live"
    assert "tool_override" not in decision.reason


# ── ADR-0306 slice 2: one decision, shared by --dry-run and the dispatcher ───


@pytest.mark.parametrize("override", [None, False, True])
@pytest.mark.parametrize("servable_match", [False, True])
@pytest.mark.parametrize("permitted", [False, True])
@pytest.mark.parametrize("permissive", [False, True])
def test_decide_intercepted_truth_table(
    override: bool | None, servable_match: bool, permitted: bool, permissive: bool
) -> None:
    """The owner's rules (2026-10-09, "Strict"), stated independently of the code."""
    action = decide_intercepted(
        override=override, servable_match=servable_match,
        permitted=permitted, permissive=permissive,
    )
    if override is True and permitted:
        expected = "live"  # Q4: a capsule permission plus the operator's flag
    elif servable_match:
        expected = "serve"  # serving is not re-execution, even for allow: false
    elif override is False:
        expected = "refuse"  # Q4: a capsule restriction holds, even under --permissive
    elif permissive and permitted:
        expected = "live"  # D7 / Q3: permissive needs the ladder flag
    else:
        expected = "refuse"
    assert action == expected


def test_an_mcp_call_is_always_gated_as_unknown() -> None:
    """A capsule's recorded class must never grant a permission (fact 9)."""
    assert gating_mutation_class("mcp.ClientSession.call_tool", "none") == "unknown"
    assert gating_mutation_class("novafabric.capture.record.tool", "read-only") == "read-only"


def test_override_report_states_what_the_replay_will_do() -> None:
    policy = {"tool_overrides": [
        {"tool_name": "gone", "allow": False},
        {"tool_name": "net", "allow": False},
        {"tool_name": "net_ok", "allow": True},
        {"tool_name": "mcp_no", "allow": False},
        {"tool_name": "mcp_yes", "allow": True,
         "rationale": "idempotent lookup, safe to re-run"},
    ]}
    calls = [
        {**_tc("net"), "transport": "http"},
        {**_tc("net_ok"), "transport": "shell"},
        {**_tc("mcp_no"), "transport": "mcp"},
        {**_tc("mcp_yes"), "transport": "mcp"},
    ]
    by_name = {
        o["tool_name"]: o
        for o in PolicyEvaluator(policy, ReplayFlags(mode="mocked")).override_report(calls)
    }
    assert by_name["gone"]["honoured"] is False
    assert by_name["gone"]["reason"].startswith("override_unused")
    assert by_name["net"]["honoured"] is False
    assert by_name["net"]["reason"].startswith("override_unenforceable")
    assert by_name["net_ok"]["honoured"] is True
    assert by_name["mcp_no"]["honoured"] is True
    assert by_name["mcp_yes"]["honoured"] is False
    assert by_name["mcp_yes"]["reason"].startswith("override_not_honoured")
    assert "--allow-unknown-mutation" in by_name["mcp_yes"]["reason"]
    assert by_name["mcp_yes"]["rationale"] == "idempotent lookup, safe to re-run"
    flagged = PolicyEvaluator(
        policy, ReplayFlags(mode="mocked", allow_unknown_mutation=True)
    ).override_report(calls)
    assert {o["tool_name"]: o["honoured"] for o in flagged}["mcp_yes"] is True
