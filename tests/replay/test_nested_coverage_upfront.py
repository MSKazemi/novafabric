"""Records covered by a served ancestor refuse nothing up front (ADR-0306 r3c).

A call recorded inside a tool call that a strict mocked replay is certain to
serve is never made at replay: the served call's body does not run. Such a
record -- an unservable inner tool or model call -- used to refuse the whole
replay up front (``ToolSurfaceNotServed``, ``ModelSurfaceNotServable``).

One pure function decides "covered" (``_contract.nested_coverage`` with
``PolicyEvaluator.certainly_served``), and the three up-front classifiers use
it: the engine (strict refusal, ``--dry-run``), the ``--dry-run`` policy report
(``PolicyEvaluator.check_all``) and the capture-time summary
(``_replayability_summary.assess``). The agreement test pins that they agree on
the same capsules. Unit tests over record dicts; no capture, no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from novafabric.capture import _tool_codec as codec
from novafabric.capture.hooks._python_tool import PAYLOADS_OFF_REASON
from novafabric.replay._contract import (
    NO_COVERAGE,
    nested_coverage,
    not_served_tool_calls,
    serves_on_match,
)
from novafabric.replay._flags import ReplayFlags
from novafabric.replay._policy import PolicyEvaluator
from novafabric.replay._replayability_summary import assess
from novafabric.replay._servability import not_servable_by_system

WITHIN = codec.WITHIN_TOOL_CALL_EXT


def _manifest() -> dict[str, Any]:
    return {
        "run_id": "01KTESTCCCCCCCCCCCCCCCCCCC",
        "status": "success",
        "command": ["python", "agent.py"],
        "capture_mode": "cli-wrapper",
        "exit_code": 0,
    }


def _boundary(
    tool_call_id: str = "t-task", *, nested: int | None = 2, reason: str | None = None,
    within: str | None = None, name: str = "task",
) -> dict[str, Any]:
    """A ``record.tool`` boundary whose body made *nested* records."""
    ext: dict[str, Any] = {codec.TOOL_SURFACE_EXT: codec.TOOL_SURFACE_PYTHON_FUNCTION}
    if nested is not None:
        ext[codec.NESTED_RECORDS_EXT] = nested
    if within is not None:
        ext[WITHIN] = within
    record: dict[str, Any] = {
        "tool_call_id": tool_call_id, "tool_name": name, "transport": "python",
        "mutation_class": "read-only", "arguments": {}, "status": "success",
    }
    if reason is None:
        ext[codec.RESULT_CODEC_EXT] = codec.CODEC_JSON
        record["result"] = {"value": "done"}
    else:
        ext[codec.RESULT_CODEC_EXT] = codec.CODEC_NOT_SERVABLE
        ext[codec.NOT_SERVABLE_REASON_EXT] = reason
        record["result"] = None
    record["extensions"] = ext
    return record


def _inner_tool(within: str | None = "t-task", name: str = "lookup") -> dict[str, Any]:
    """An observed LangChain call the replay cannot serve (payloads off, no echo)."""
    ext: dict[str, Any] = {
        codec.TOOL_SURFACE_EXT: codec.TOOL_SURFACE_LANGCHAIN_TOOL,
        codec.LANGCHAIN_SEAM_EXT: codec.LANGCHAIN_SEAM_OBSERVED,
        codec.RESULT_CODEC_EXT: codec.CODEC_NOT_SERVABLE,
        codec.NOT_SERVABLE_REASON_EXT: "the result is a set, not JSON",
    }
    if within is not None:
        ext[WITHIN] = within
    return {
        "tool_call_id": f"lc-{name}", "tool_name": name, "transport": "python",
        "mutation_class": "unknown", "arguments": {"q": 1}, "status": "success",
        "result": None, "extensions": ext,
    }


def _inner_model(within: str | None = "t-task") -> dict[str, Any]:
    """A model call on a surface replay does not serve (ADR-0319)."""
    record: dict[str, Any] = {
        "model_call_id": "mistral-1", "gen_ai.system": "mistral",
        "endpoint": "https://api.mistral.ai/v1/chat/completions",
        "status": "success", "gen_ai.response.choices": [],
    }
    if within is not None:
        record["extensions"] = {WITHIN: within}
    return record


def _strict() -> PolicyEvaluator:
    return PolicyEvaluator({}, ReplayFlags(mode="mocked"))


# ── the shared predicate ─────────────────────────────────────────────────────


def test_a_record_inside_a_served_boundary_is_covered_by_it() -> None:
    outer, inner = _boundary(), _inner_tool()
    coverage = _strict().coverage([outer, inner])
    assert coverage.covering(inner) is outer
    assert coverage.covering(_inner_model()) is outer
    assert coverage.covering(outer) is None  # the boundary itself is served, not covered
    # The pure function with its default predicate (servable on a match) agrees.
    assert nested_coverage([outer, inner]).covering(inner) is outer
    assert nested_coverage([outer, inner], lambda _r: False).covering(inner) is None


def test_the_outermost_served_ancestor_covers_through_an_unservable_middle() -> None:
    outer = _boundary("t-outer", nested=3, name="outer")
    middle = _boundary("t-mid", nested=1, reason=PAYLOADS_OFF_REASON, within="t-outer",
                       name="middle")
    inner = _inner_tool(within="t-mid")
    coverage = _strict().coverage([outer, middle, inner])
    assert coverage.covering(inner) is outer
    assert coverage.covering(middle) is outer


@pytest.mark.parametrize("why, records, inner", [
    ("the outer record is not servable",
     [_boundary(reason=PAYLOADS_OFF_REASON)], _inner_tool()),
    ("the outer record counts no nested records",
     [_boundary(nested=None)], _inner_tool()),
    ("the ancestor id is held by no record",
     [_boundary()], _inner_tool(within="t-nope")),
    ("the ancestor id is held by two records (ambiguous)",
     [_boundary(), _boundary()], _inner_tool()),
    ("the record names no ancestor",
     [_boundary()], _inner_tool(within=None)),
])
def test_missing_ambiguous_or_unservable_ancestry_covers_nothing(
    why: str, records: list[dict[str, Any]], inner: dict[str, Any],
) -> None:
    assert _strict().coverage([*records, inner]).covering(inner) is None, why


def test_a_break_anywhere_up_the_chain_covers_nothing() -> None:
    """A served parent whose own ancestry is broken: fail closed, not covered."""
    middle = _boundary("t-mid", nested=1, within="t-gone", name="middle")
    inner = _inner_tool(within="t-mid")
    assert _strict().coverage([middle, inner]).covering(inner) is None


def test_a_cycle_covers_nothing() -> None:
    a = _boundary("t-a", nested=1, within="t-b", name="a")
    b = _boundary("t-b", nested=1, within="t-a", name="b")
    inner = _inner_tool(within="t-a")
    assert _strict().coverage([a, b, inner]).covering(inner) is None


def test_an_adk_failure_never_covers_a_python_failure_does() -> None:
    """The python server re-raises a recorded failure (and covers); the
    framework seams never serve a failure."""
    py_fail = _boundary()
    py_fail["status"] = "error"
    assert serves_on_match(py_fail) is True
    lc_outer = _inner_tool(within=None, name="task")
    lc_outer["extensions"].update({
        codec.RESULT_CODEC_EXT: codec.CODEC_JSON, codec.NESTED_RECORDS_EXT: 1,
    })
    lc_outer["result"] = {"value": "x"}
    assert serves_on_match(lc_outer) is True
    lc_outer["status"] = "error"
    assert serves_on_match(lc_outer) is False


# ── the flags and replay.yaml decide "certain to be served" ─────────────────


def _policy(allow: bool) -> dict[str, Any]:
    return {"schema_version": "0.1.0",
            "tool_overrides": [{"tool_name": "task", "allow": allow}]}


@pytest.mark.parametrize("policy, flags, covered", [
    (None, ReplayFlags(mode="mocked"), True),
    # allow: false refuses an unmatched call and still serves a matched one.
    (_policy(False), ReplayFlags(mode="mocked"), True),
    # allow: true re-executes the body when the ladder permits it: not certain.
    (_policy(True), ReplayFlags(mode="mocked"), False),
    (_policy(True), ReplayFlags(mode="mocked", allow_unknown_mutation=True), False),
    # --permissive runs an unmatched wrapped call live whenever the ladder permits
    # its class, and the class is the workload's at replay: not certain.
    (None, ReplayFlags(mode="mocked", permissive=True), False),
    # Other modes install no tool server: nothing is covered.
    (None, ReplayFlags(mode="forensic"), False),
])
def test_certainly_served_follows_the_replay_decision(
    policy: dict[str, Any] | None, flags: ReplayFlags, covered: bool,
) -> None:
    outer, inner = _boundary(), _inner_tool()
    coverage = PolicyEvaluator(policy or {}, flags).coverage([outer, inner])
    assert coverage.is_covered(inner) is covered


def test_an_observed_outer_under_permissive_is_covered_only_without_the_unknown_flag() -> None:
    """Replay gates an observed tool as ``unknown`` whatever the capsule says, so
    --permissive runs an unmatched one live only with --allow-unknown-mutation."""
    outer = _inner_tool(within=None, name="task")
    outer["tool_call_id"] = "t-task"
    outer["extensions"].update({
        codec.RESULT_CODEC_EXT: codec.CODEC_JSON, codec.NESTED_RECORDS_EXT: 1,
    })
    outer["extensions"].pop(codec.NOT_SERVABLE_REASON_EXT)
    outer["result"] = {"value": "x"}
    inner = _inner_tool()
    permissive = ReplayFlags(mode="mocked", permissive=True)
    assert PolicyEvaluator({}, permissive).coverage([outer, inner]).is_covered(inner)
    unknown = ReplayFlags(mode="mocked", permissive=True, allow_unknown_mutation=True)
    assert not PolicyEvaluator({}, unknown).coverage([outer, inner]).is_covered(inner)


def test_the_classifiers_drop_covered_records_and_keep_the_rest() -> None:
    outer, inner = _boundary(), _inner_tool()
    coverage = _strict().coverage([outer, inner])
    assert not_served_tool_calls([outer, inner], coverage) == {}
    assert not_served_tool_calls([outer, inner], NO_COVERAGE) != {}
    assert not_served_tool_calls([outer, inner]) == not_served_tool_calls(
        [outer, inner], NO_COVERAGE
    )
    assert not_servable_by_system([_inner_model()], coverage) == {}
    assert not_servable_by_system([_inner_model(within="t-nope")], coverage) != {}


# ── the three classifiers agree on the same capsules ─────────────────────────


def _write_capsule(
    root: Path, models: list[dict[str, Any]], tools: list[dict[str, Any]],
    policy: dict[str, Any] | None,
) -> Path:
    capsule = root / "capsule"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(_manifest()))
    (capsule / "model-calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in models))
    (capsule / "tool-calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in tools))
    (capsule / "replay.yaml").write_text(yaml.safe_dump(policy or {"schema_version": "0.1.0"}))
    return capsule


#: (models, tools, replay.yaml, strict refusal type, covered tools, covered models)
_CASES: dict[str, tuple[list[Any], list[Any], Any, str | None, int, int]] = {
    "inner tool and model covered": (
        [_inner_model()], [_boundary(), _inner_tool()], None, None, 1, 1),
    "inner tool covered": ([], [_boundary(), _inner_tool()], None, None, 1, 0),
    "inner model covered": ([_inner_model()], [_boundary(nested=1)], None, None, 0, 1),
    "chain through an unservable middle": (
        [_inner_model(within="t-mid")],
        [_boundary("t-outer", nested=3, name="outer"),
         _boundary("t-mid", nested=2, reason=PAYLOADS_OFF_REASON, within="t-outer",
                   name="middle"),
         _inner_tool(within="t-mid")], None, None, 2, 1),
    "allow: false still serves the outer": (
        [_inner_model()], [_boundary(), _inner_tool()], _policy(False), None, 1, 1),
    "outer not servable": (
        [_inner_model()], [_boundary(reason=PAYLOADS_OFF_REASON), _inner_tool()], None,
        "ModelSurfaceNotServable", 0, 0),
    "outer not servable, tool only": (
        [], [_boundary(reason=PAYLOADS_OFF_REASON), _inner_tool()], None,
        "ToolSurfaceNotServed", 0, 0),
    "ambiguous ancestry": (
        [_inner_model()], [_boundary(), _boundary(), _inner_tool()], None,
        "ModelSurfaceNotServable", 0, 0),
    "missing ancestry": (
        [], [_boundary(), _inner_tool(within="t-nope")], None, "ToolSurfaceNotServed", 0, 0),
    "allow: true may re-execute the outer": (
        [], [_boundary(), _inner_tool()], _policy(True), "ToolSurfaceNotServed", 0, 0),
}


@pytest.mark.parametrize("name", list(_CASES))
def test_engine_dry_run_policy_report_and_summary_agree(tmp_path: Path, name: str) -> None:
    from novafabric.replay._engine import ReplayEngine

    models, tools, policy, refusal, covered_tools, covered_models = _CASES[name]
    capsule = _write_capsule(tmp_path, models, tools, policy)
    result = ReplayEngine(
        capsule, ReplayFlags(mode="mocked", dry_run=True), base_dir=tmp_path / "replays"
    ).run()
    report = (tmp_path / "replays" / result.replay_id / "dry_run_report.txt").read_text()
    doc = assess(_manifest(), models, tools, replay_policy=policy).as_dict()
    decisions = PolicyEvaluator(policy or {}, ReplayFlags(mode="mocked")).check_all(tools)

    # 1. The engine and the summary refuse alike (type and message), or neither.
    assert (result.error or {}).get("type") == refusal
    assert (doc["refusal"] or {}).get("type") == refusal
    if refusal is not None:
        assert doc["refusal"]["message"] == result.error["message"]  # type: ignore[index]
    # 2. The same unservable model-call count; covered ones are in neither.
    assert doc["model_calls"]["not_servable"] == (result.model_calls_not_servable or 0)
    assert doc["model_calls"]["covered"] == covered_models
    # 3. The policy report marks exactly the covered tool records as covered.
    assert sum(1 for d in decisions if d.decision == "covered") == covered_tools
    assert doc["tool_calls"]["covered"] == covered_tools
    served = sum(1 for d in decisions if d.decision == "mock")
    assert doc["tool_calls"]["servable"] == served
    # 4. The dry run lists each covered record as covered, never MOCK/LIVE/DENY.
    assert report.count("[COVERED by ") == covered_tools + covered_models
    for d in decisions:
        if d.decision == "covered":
            assert f"[COVERED by {d.label_note}] {d.tool_name} " in report


def test_the_summary_says_full_when_only_covered_records_are_unservable() -> None:
    doc = assess(_manifest(), [_inner_model()], [_boundary(), _inner_tool()]).as_dict()
    assert doc["replayable"] == "full"
    assert doc["strict_mocked"] == "start"
    assert doc["reasons"] == []
    assert doc["tool_calls"] == {
        "recorded": 2, "servable": 1, "not_servable": 0, "observed_only": 0,
        "not_intercepted": 0, "covered": 1,
    }
    assert doc["model_calls"] == {
        "recorded": 1, "servable": 0, "not_servable": 0, "live": 0, "covered": 1,
    }


def test_the_covering_surfaces_are_exactly_the_servers_that_cover_on_serve() -> None:
    """The up-front classifiers assume a served call on these surfaces covers what
    its body wrote; the dispatcher must pass ``on_served`` to exactly those."""
    from novafabric.replay._contract import (
        COVERING_TOOL_SURFACES,
        TOOL_SURFACE_ADK,
        TOOL_SURFACE_LANGCHAIN,
        TOOL_SURFACE_OPENAI_AGENTS,
        TOOL_SURFACE_PYTHON,
    )
    from novafabric.replay._dispatcher import MockToolDispatcher

    dispatcher = MockToolDispatcher([])
    servers = {
        TOOL_SURFACE_PYTHON: dispatcher._python,
        TOOL_SURFACE_ADK: dispatcher._adk,
        TOOL_SURFACE_OPENAI_AGENTS: dispatcher._oai,
        TOOL_SURFACE_LANGCHAIN: dispatcher._langchain,
    }
    covering = {s for s, server in servers.items() if server._on_served is not None}
    assert covering == COVERING_TOOL_SURFACES
