"""Replayability at capture: the replayability summary a capsule carries from the moment it is written.

Users learned whether a capsule replays only at replay time (``ModelSurfaceNotServable``,
``ToolSurfaceNotServed``, ``CapsuleNotReplayable``, a ``tool_result_not_servable``
divergence). The summary classifies every recorded call with the *same* predicates the
replay engine and its ``--dry-run`` use (``_servability``, ``_contract``,
``_replayability``, ``PolicyEvaluator``) -- the agreement tests at the bottom pin that.

Unit tests over record dicts; no capture, no network.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from novafabric.capture import _tool_codec as codec
from novafabric.capture.hooks._python_tool import PAYLOADS_OFF_REASON
from novafabric.replay._flags import ReplayFlags
from novafabric.replay._replayability_summary import (
    REPLAYABILITY_REF,
    REPLAYABILITY_SCHEMA,
    InvalidReplayabilityError,
    assess,
    parse_replayability,
    read_replayability,
    render,
)


def _cli_manifest() -> dict[str, Any]:
    return {
        "run_id": "01KTESTAAAAAAAAAAAAAAAAAAA",
        "status": "success",
        "command": ["python", "agent.py"],
        "capture_mode": "cli-wrapper",
        "exit_code": 0,
    }


def _adapter_manifest(facet: dict[str, Any] | None) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "run_id": "01KTESTBBBBBBBBBBBBBBBBBBB",
        "status": "success",
        "command": ["@langgraph:code-reviewer"],
        "capture_mode": "sdk-decorator",
        "exit_code": 0,
    }
    if facet is not None:
        manifest["facets"] = {"process_invocation": facet}
    return manifest


def _openai(content: str = "hi") -> dict[str, Any]:
    return {
        "model_call_id": f"m-{content}",
        "gen_ai.system": "openai",
        "status": "success",
        "gen_ai.response.choices": [{"index": 0, "message": {"content": content}}],
    }


def _mistral(i: int) -> dict[str, Any]:
    return {
        "model_call_id": f"mistral-{i}",
        "gen_ai.system": "mistral",
        "endpoint": "https://api.mistral.ai/v1/chat/completions",
        "status": "success",
        "gen_ai.response.choices": [],
    }


def _ollama_tags() -> dict[str, Any]:
    return {
        "model_call_id": "tags",
        "gen_ai.system": "ollama",
        "endpoint": "http://127.0.0.1:11434/api/tags",
        "status": "success",
        "gen_ai.response.choices": [],
    }


def _python_tool(name: str, *, reason: str | None = None) -> dict[str, Any]:
    ext: dict[str, Any] = {codec.TOOL_SURFACE_EXT: codec.TOOL_SURFACE_PYTHON_FUNCTION}
    record: dict[str, Any] = {
        "tool_call_id": f"t-{name}",
        "tool_name": name,
        "transport": "python",
        "mutation_class": "read-only",
        "arguments": {},
        "status": "success",
    }
    if reason is None:
        ext[codec.RESULT_CODEC_EXT] = codec.CODEC_JSON
        record["result"] = {"value": 1}
    else:
        ext[codec.RESULT_CODEC_EXT] = codec.CODEC_NOT_SERVABLE
        ext[codec.NOT_SERVABLE_REASON_EXT] = reason
        record["result"] = None
    record["extensions"] = ext
    return record


def _langchain_observed(name: str, *, payloads_off: bool = False) -> dict[str, Any]:
    record: dict[str, Any] = {
        "tool_call_id": f"lc-{name}",
        "tool_name": name,
        "transport": "python",
        "mutation_class": "unknown",
        "arguments": {"q": 1},
        "status": "success",
        "result": {"value": "x"},
        "extensions": {
            codec.TOOL_SURFACE_EXT: codec.TOOL_SURFACE_LANGCHAIN_TOOL,
            codec.LANGCHAIN_SEAM_EXT: codec.LANGCHAIN_SEAM_OBSERVED,
            codec.RESULT_CODEC_EXT: codec.CODEC_JSON,
        },
    }
    if payloads_off:
        digest = codec.encode_result("x", keep_value=False).digest
        record["arguments"] = {}
        record["result"] = None
        record["extensions"].update({
            LC_CALL_ID_EXT: f"tooluse-{name}",
            "io.novafabric.result_digest": digest,
            codec.RESULT_CODEC_EXT: "not-servable",
            codec.NOT_SERVABLE_REASON_EXT: PAYLOADS_OFF_REASON,
        })
    return record


LC_CALL_ID_EXT = "io.novafabric.langchain_tool_call_id"


def _langchain_legacy_nested(name: str) -> dict[str, Any]:
    """An observed LangChain record an earlier capture marked not servable only
    because its body made nested calls (ADR-0306 r3b strips that text)."""
    record = _langchain_observed(name)
    record["extensions"].update({
        codec.NESTED_RECORDS_EXT: 2,
        codec.RESULT_CODEC_EXT: codec.CODEC_NOT_SERVABLE,
        codec.NOT_SERVABLE_REASON_EXT: codec.seam_nested_reason("LangChain tool", 2),
    })
    return record


def _bedrock_echo(call_id: str, text: str) -> dict[str, Any]:
    """A recorded Bedrock request that sends *text* back as the result of *call_id*."""
    return {
        "model_call_id": f"m-{call_id}", "gen_ai.system": "aws.bedrock",
        "endpoint": "https://bedrock-runtime.eu-central-1.amazonaws.com/model/x/converse",
        "gen_ai.response.choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "extensions": {"io.novafabric.api_surface": "aws.bedrock.converse"},
        "gen_ai.request.messages": [
            {"role": "user", "content": [{"text": "go"}]},
            {"role": "assistant", "content": [{"toolUse": {"toolUseId": call_id}}]},
            {"role": "user", "content": [{"toolResult": {
                "toolUseId": call_id, "content": [{"text": text}], "status": "success",
            }}]},
        ],
    }


def _http_tool(name: str) -> dict[str, Any]:
    return {"tool_call_id": f"h-{name}", "tool_name": name, "transport": "http",
            "mutation_class": "unknown", "arguments": {}, "status": "success"}


def _codes(summary: Any) -> list[str]:
    return [r["code"] for r in summary.as_dict()["reasons"]]


# ── verdicts ────────────────────────────────────────────────────────────────


def test_a_served_openai_run_is_fully_replayable() -> None:
    summary = assess(_cli_manifest(), [_openai("a"), _openai("b")], [_python_tool("add")])
    doc = summary.as_dict()
    assert doc["schema"] == REPLAYABILITY_SCHEMA
    assert doc["replayable"] == "full"
    assert doc["strict_mocked"] == "start"
    assert doc["refusal"] is None
    assert doc["reasons"] == []
    assert doc["model_calls"] == {"recorded": 2, "servable": 2, "not_servable": 0, "live": 0}
    assert doc["tool_calls"]["recorded"] == 1
    assert doc["tool_calls"]["servable"] == 1
    assert summary.one_line() == (
        "replay: strict mocked replay would serve every recorded call (2 model, 1 tool)"
    )


def test_a_capsule_with_no_calls_is_fully_replayable() -> None:
    summary = assess(_cli_manifest(), [], [])
    assert summary.replayable == "full"
    assert summary.strict_mocked == "start"


def test_mistral_calls_make_strict_replay_refuse_with_the_reason() -> None:
    summary = assess(_cli_manifest(), [_openai(), _mistral(1), _mistral(2), _mistral(3)], [])
    doc = summary.as_dict()
    assert doc["replayable"] == "partial"
    assert doc["strict_mocked"] == "refuse"
    assert doc["refusal"]["type"] == "ModelSurfaceNotServable"
    (reason,) = doc["reasons"]
    assert reason["code"] == "model_surface_not_servable"
    assert reason["count"] == 3
    assert reason["refuses_strict"] is True
    assert reason["by_system"] == {"mistral": {"count": 3, "hosts": ["api.mistral.ai"]}}
    assert doc["model_calls"] == {"recorded": 4, "servable": 1, "not_servable": 3, "live": 0}
    line = summary.one_line()
    assert line.startswith("replay: strict mocked replay would refuse")
    assert "3 model calls not servable (mistral: 3 (api.mistral.ai))" in line
    assert "nova replay --dry-run" in line


def test_record_tool_payloads_off_is_named_and_strict_starts_but_diverges() -> None:
    summary = assess(
        _cli_manifest(), [_openai()],
        [_python_tool("add", reason=PAYLOADS_OFF_REASON),
         _python_tool("mul", reason=PAYLOADS_OFF_REASON), _python_tool("ok")],
    )
    doc = summary.as_dict()
    assert doc["replayable"] == "partial"
    assert doc["strict_mocked"] == "start"
    assert doc["refusal"] is None
    (reason,) = doc["reasons"]
    assert reason["code"] == "tool_result_not_servable"
    assert reason["cause"] == "payload_not_recorded"
    assert reason["count"] == 2
    assert reason["tools"] == ["add", "mul"]
    assert reason["refuses_strict"] is False
    assert "NOVA_CAPTURE_LEVEL=forensic" in reason["detail"]
    assert doc["tool_calls"]["servable"] == 1
    assert doc["tool_calls"]["not_servable"] == 2
    assert "2 tool calls whose payload was not recorded" in summary.one_line()


def test_another_unservable_tool_result_keeps_its_recorded_reason() -> None:
    why = "the result is 9 bytes, over the 4-byte cap (NOVAFABRIC_TOOL_RESULT_MAX_BYTES)"
    summary = assess(_cli_manifest(), [], [_python_tool("big", reason=why)])
    (reason,) = summary.as_dict()["reasons"]
    assert reason["code"] == "tool_result_not_servable"
    assert reason["cause"] == "other"
    assert why in reason["detail"]


def test_an_observed_langchain_tool_with_its_result_is_served() -> None:
    # ADR-0306 r3: observed (unwrapped) tools are served when their value was kept.
    summary = assess(_cli_manifest(), [], [_langchain_observed("read_log")])
    doc = summary.as_dict()
    assert doc["strict_mocked"] == "start"
    assert "tool_surface_not_served" not in _codes(summary)


def test_a_payloads_off_observed_tool_echoed_under_its_digest_is_served() -> None:
    # The engine recovers it from the next model request (ADR-0306 r3, "A");
    # the capture-time summary must agree with that replay.
    tool = _langchain_observed("read_log", payloads_off=True)
    summary = assess(_cli_manifest(), [_bedrock_echo("tooluse-read_log", "x")], [tool])
    assert summary.as_dict()["strict_mocked"] == "start"
    assert "tool_surface_not_served" not in _codes(summary)


def test_a_legacy_nested_langchain_record_with_its_value_is_served() -> None:
    summary = assess(_cli_manifest(), [], [_langchain_legacy_nested("task")])
    doc = summary.as_dict()
    assert doc["strict_mocked"] == "start" and doc["replayable"] == "full"
    assert doc["tool_calls"]["servable"] == 1


def test_an_observed_langchain_tool_refuses_strict() -> None:
    summary = assess(_cli_manifest(), [], [_langchain_observed("read_log", payloads_off=True)])
    doc = summary.as_dict()
    assert doc["strict_mocked"] == "refuse"
    assert doc["refusal"]["type"] == "ToolSurfaceNotServed"
    assert _codes(summary) == ["tool_surface_not_served"]
    assert doc["reasons"][0]["tools"] == ["read_log"]
    assert doc["tool_calls"]["observed_only"] == 1


def test_a_tool_on_a_transport_replay_does_not_intercept_runs_live() -> None:
    summary = assess(_cli_manifest(), [], [_http_tool("fetch")])
    doc = summary.as_dict()
    assert doc["replayable"] == "partial"
    assert doc["strict_mocked"] == "start"
    assert _codes(summary) == ["tool_not_intercepted"]
    assert doc["reasons"][0]["by_transport"] == {"http": 1}
    assert doc["tool_calls"]["not_intercepted"] == 1


def test_an_ollama_metadata_call_is_reported_as_running_live() -> None:
    summary = assess(_cli_manifest(), [_ollama_tags()], [])
    doc = summary.as_dict()
    assert doc["replayable"] == "partial"
    assert doc["strict_mocked"] == "start"
    assert _codes(summary) == ["model_metadata_call_live"]
    assert doc["model_calls"]["live"] == 1


def test_transport_records_are_neither_served_nor_counted() -> None:
    wire = {
        "model_call_id": "w1", "gen_ai.system": "openai", "endpoint": "https://x/v1/chat",
        "gen_ai.response.choices": [],
        "extensions": {"io.novafabric.record_role": "transport",
                       "io.novafabric.logical_call_id": "m-hi"},
    }
    logical = _openai()
    logical["extensions"] = {"io.novafabric.record_role": "logical",
                             "io.novafabric.logical_call_id": "m-hi"}
    summary = assess(_cli_manifest(), [wire, logical], [])
    assert summary.replayable == "full"
    assert summary.as_dict()["model_calls"]["recorded"] == 1


# ── re-execution (adapter capsules) ──────────────────────────────────────────


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _facet(project: Path, **overrides: Any) -> dict[str, Any]:
    script = project / "agent.py"
    facet: dict[str, Any] = {
        "schema_version": "0.1.0",
        "argv": ["/usr/bin/python3", "agent.py"],
        "entry_kind": "script",
        "entry": "agent.py",
        "entry_file": str(script),
        "entry_sha256": _sha(script),
        "cwd": str(project),
        "stdin_isatty": False,
        "adapter_run_ordinal": 1,
        "host_modules": [],
    }
    facet.update(overrides)
    return facet


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "agent.py").write_text("print('agent')\n")
    return tmp_path


def test_a_runpy_style_adapter_entry_is_not_reproducible(project: Path) -> None:
    # A launcher that rewrote sys.argv (runpy.run_path) is classified `unknown`.
    facet = _facet(project, entry_kind="unknown", entry=None, entry_file=None,
                   entry_sha256=None)
    summary = assess(_adapter_manifest(facet), [_openai()], [])
    doc = summary.as_dict()
    assert doc["replayable"] == "none"
    assert doc["strict_mocked"] == "refuse"
    assert doc["refusal"]["type"] == "CapsuleNotReplayable"
    assert doc["refusal"]["guard"] == "entry_not_reproducible"
    (reason,) = doc["reasons"]
    assert reason["code"] == "capsule_not_reexecutable"
    assert reason["guard"] == "entry_not_reproducible"
    assert doc["reexecution"] == {"reexecutable": False, "source": None,
                                  "guard": "entry_not_reproducible"}
    assert "guard: entry_not_reproducible" in summary.one_line()


def test_an_adapter_capsule_passing_every_guard_is_reexecutable(project: Path) -> None:
    summary = assess(_adapter_manifest(_facet(project)), [_openai()], [])
    assert summary.replayable == "full"
    assert summary.as_dict()["reexecution"] == {
        "reexecutable": True, "source": "process_invocation", "guard": None,
    }


def test_an_adapter_capsule_without_the_facet_is_not_reexecutable() -> None:
    summary = assess(_adapter_manifest(None), [_openai()], [])
    doc = summary.as_dict()
    assert doc["replayable"] == "none"
    assert doc["refusal"]["type"] == "CapsuleNotReplayable"
    assert doc["refusal"].get("guard") is None


def test_every_reason_is_listed_even_when_the_first_one_refuses(project: Path) -> None:
    facet = _facet(project, entry_kind="stdin", entry=None, entry_file=None,
                   entry_sha256=None)
    summary = assess(
        _adapter_manifest(facet), [_mistral(1)],
        [_langchain_observed("t", payloads_off=True),
         _python_tool("p", reason=PAYLOADS_OFF_REASON)],
    )
    assert _codes(summary) == [
        "capsule_not_reexecutable", "model_surface_not_servable",
        "tool_surface_not_served", "tool_result_not_servable",
    ]
    # The refusal is the one the replay reports: the first in the engine's order.
    assert summary.as_dict()["refusal"]["type"] == "CapsuleNotReplayable"


def test_an_unenforceable_override_refuses_strict() -> None:
    policy = {"schema_version": "0.1.0",
              "tool_overrides": [{"tool_name": "fetch", "allow": False}]}
    summary = assess(_cli_manifest(), [], [_http_tool("fetch")], replay_policy=policy)
    doc = summary.as_dict()
    assert doc["strict_mocked"] == "refuse"
    assert doc["refusal"]["type"] == "ToolOverrideUnenforceable"
    assert "tool_override_unenforceable" in _codes(summary)


# ── the file ─────────────────────────────────────────────────────────────────


def test_render_round_trips_and_is_stable() -> None:
    summary = assess(_cli_manifest(), [_mistral(1)], [_http_tool("x")])
    text = render(summary)
    assert text == render(summary)
    assert text.endswith("\n")
    assert parse_replayability(text.encode()) == summary.as_dict()


@pytest.mark.parametrize("bad", [
    b"not json", b"[]", b'{"schema": "other/1"}',
    b'{"schema": "novafabric.replayability/1", "replayable": "maybe", "strict_mocked": "start"}',
    b'{"schema": "novafabric.replayability/1", "replayable": "full", "strict_mocked": "go"}',
])
def test_a_malformed_file_is_refused(bad: bytes) -> None:
    with pytest.raises(InvalidReplayabilityError):
        parse_replayability(bad)


def test_an_old_capsule_without_the_file_reads_as_unknown(tmp_path: Path) -> None:
    assert read_replayability(tmp_path) is None


def test_read_replayability_reads_the_written_file(tmp_path: Path) -> None:
    summary = assess(_cli_manifest(), [], [])
    (tmp_path / REPLAYABILITY_REF).write_text(render(summary))
    assert read_replayability(tmp_path) == summary.as_dict()


# ── agreement with `nova replay --dry-run` ───────────────────────────────────


def _write_capsule(
    root: Path, manifest: dict[str, Any], models: list[dict[str, Any]],
    tools: list[dict[str, Any]], policy: dict[str, Any] | None = None,
) -> Path:
    capsule = root / "capsule"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    (capsule / "model-calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in models))
    (capsule / "tool-calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in tools))
    (capsule / "replay.yaml").write_text(yaml.safe_dump(policy or {"schema_version": "0.1.0"}))
    return capsule


def _cases(project: Path) -> list[tuple[dict[str, Any], list[Any], list[Any], Any]]:
    unknown = _facet(project, entry_kind="unknown", entry=None, entry_file=None,
                     entry_sha256=None)
    deny = {"schema_version": "0.1.0", "tool_overrides": [{"tool_name": "x", "allow": False}]}
    return [
        (_cli_manifest(), [_openai()], [_python_tool("ok")], None),
        (_cli_manifest(), [_mistral(1), _mistral(2)], [], None),
        (_cli_manifest(), [], [_langchain_observed("t")], None),
        (_cli_manifest(), [_openai()], [_python_tool("p", reason=PAYLOADS_OFF_REASON)], None),
        (_cli_manifest(), [], [_http_tool("x")], deny),
        (_cli_manifest(), [_ollama_tags()], [_http_tool("y")], None),
        (_adapter_manifest(unknown), [_mistral(1)], [], None),
        (_adapter_manifest(_facet(project)), [_openai()], [], None),
        (_adapter_manifest(None), [], [], None),
        (_cli_manifest(), [], [_langchain_legacy_nested("task")], None),
    ]


@pytest.mark.parametrize("case", range(10))
def test_the_summary_agrees_with_the_dry_run(
    tmp_path: Path, project: Path, case: int,
) -> None:
    from novafabric.replay._engine import ReplayEngine
    from novafabric.replay._policy import PolicyEvaluator

    manifest, models, tools, policy = _cases(project)[case]
    capsule = _write_capsule(tmp_path, manifest, models, tools, policy)
    result = ReplayEngine(
        capsule, ReplayFlags(mode="mocked", dry_run=True), base_dir=tmp_path / "replays"
    ).run()
    summary = assess(manifest, models, tools, replay_policy=policy)
    doc = summary.as_dict()

    # 1. Strict refusal: the same error (type and message), or none.
    refused = (result.error or {}).get("type")
    assert (doc["refusal"] or {}).get("type") == refused
    assert (doc["strict_mocked"] == "refuse") == (refused is not None)
    if refused is not None:
        assert doc["refusal"]["message"] == result.error["message"]  # type: ignore[index]
    # 2. The same unservable model-call count.
    assert doc["model_calls"]["not_servable"] == (result.model_calls_not_servable or 0)
    # 3. Per tool call, the decision the dry-run policy report prints.
    decisions = PolicyEvaluator(policy or {}, ReplayFlags(mode="mocked")).check_all(tools)
    served = sum(1 for d in decisions if d.decision == "mock")
    assert doc["tool_calls"]["servable"] == served
    uncontrolled = sum(1 for d in decisions if "is not intercepted by replay" in d.reason)
    assert doc["tool_calls"]["not_intercepted"] == uncontrolled
    # 4. `full` exactly when the dry run would neither refuse nor run anything live.
    assert (doc["replayable"] == "full") == (
        refused is None and served == len(tools) and doc["model_calls"]["live"] == 0
    )
