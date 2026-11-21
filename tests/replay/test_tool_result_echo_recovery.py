"""Tool results recovered from the next recorded model request (ADR-0306 r3, "A").

A tool record captured below ``forensic`` keeps no result, only
``io.novafabric.result_digest`` (sha256 over the secret-redacted canonical JSON
of the value). The result was also sent to the model in the next request,
which the capsule does keep. ``recover_tool_results_from_echo`` serves that
echo **only** when one of its readings has exactly the recorded digest,
computed by the capture codec itself; anything else leaves the record as it was.

Shapes: c08's hpc_analyst / iot_detective / code_reviewer (LangChain over
Bedrock Converse, ``toolResult`` text), plus OpenAI Chat ``role: tool``,
Anthropic ``tool_result`` and the Responses API ``function_call_output``.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from novafabric.capture import _tool_codec as codec
from novafabric.capture.hooks._python_tool import PAYLOADS_OFF_REASON
from novafabric.replay._contract import not_servable_reason
from novafabric.replay._echo_recovery import (
    SOURCE_EXT,
    SOURCE_MODEL_REQUEST_ECHO,
    recover_tool_results_from_echo,
)

LC_ID = "io.novafabric.langchain_tool_call_id"


def _digest(value: Any) -> str:
    digest = codec.encode_result(value, keep_value=False).digest
    assert digest is not None
    return digest


def _tool_record(
    value: Any, call_id: str = "tooluse_A", *, name: str = "read_log",
    surface: str = "langchain.tool", id_ext: str = LC_ID, **ext: Any,
) -> dict[str, Any]:
    return {
        "tool_call_id": f"rec-{call_id}", "tool_name": name, "transport": "python",
        "mutation_class": "unknown", "arguments": {}, "result": None, "status": "success",
        "extensions": {
            "io.novafabric.tool_surface": surface,
            id_ext: call_id,
            "io.novafabric.arguments_digest": "abc",
            "io.novafabric.result_digest": _digest(value),
            "io.novafabric.result_codec": "not-servable",
            "io.novafabric.not_servable_reason": PAYLOADS_OFF_REASON,
            "io.novafabric.langchain_tool_seam": "observed",
            **ext,
        },
    }


def _bedrock(call_id: str, text: str, status: str = "success") -> dict[str, Any]:
    return {
        "model_call_id": f"m-{call_id}", "gen_ai.system": "aws.bedrock",
        "gen_ai.request.messages": [
            {"role": "user", "content": [{"text": "go"}]},
            {"role": "assistant", "content": [{"toolUse": {"toolUseId": call_id}}]},
            {"role": "user", "content": [{"toolResult": {
                "toolUseId": call_id, "content": [{"text": text}], "status": status,
            }}]},
        ],
    }


def _served(record: dict[str, Any]) -> Any:
    assert not_servable_reason(record) is None, not_servable_reason(record)
    ok, value = codec.decode_result(record)
    assert ok
    return value


# ── the c08 shapes ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "slurmstepd: error: Detected 1 oom_kill event in StepId=42819.batch\n",  # hpc read_log
    "saved output/job-42819-diagnosis.json",  # hpc write_diagnosis
    "stdout:\nanomalies=3 (sensor-07, sensor-11, sensor-19)\n",  # iot execute_python
])
def test_a_string_result_echoed_in_a_bedrock_tool_result_is_served(text: str) -> None:
    tools = [_tool_record(text)]
    models = [_bedrock("tooluse_A", text)]
    (out,) = recover_tool_results_from_echo(tools, models)
    assert _served(out) == text
    assert out["extensions"][SOURCE_EXT] == SOURCE_MODEL_REQUEST_ECHO
    assert out["extensions"]["io.novafabric.result_source_model_call_id"] == "m-tooluse_A"
    assert tools[0]["result"] is None, "the input records are never modified"


def test_a_dict_result_is_served_as_the_dict_whose_digest_matched() -> None:
    """LangChain sends a dict result to the model as ``json.dumps``; the served
    value is the canonical value whose digest matched -- a dict, not the text."""
    value = {"job_id": "42819", "severity": "HIGH", "lines": [3, 7]}
    tools = [_tool_record(value)]
    models = [_bedrock("tooluse_A", json.dumps(value))]
    (out,) = recover_tool_results_from_echo(tools, models)
    assert _served(out) == value


# ── other providers, through the same parser as the D10 echo check ───────────


def test_openai_chat_anthropic_and_responses_echoes_are_read() -> None:
    tools = [
        _tool_record("from-openai", "call_O"),
        _tool_record("from-anthropic", "toolu_A"),
        _tool_record("from-responses", "call_R"),
    ]
    models = [
        {"model_call_id": "m1", "gen_ai.request.messages": [
            {"role": "tool", "tool_call_id": "call_O", "content": "from-openai"},
        ]},
        {"model_call_id": "m2", "gen_ai.request.messages": [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_A", "content": "from-anthropic"},
            ]},
        ]},
        {"model_call_id": "m3", "gen_ai.request.messages": [
            {"type": "function_call_output", "call_id": "call_R", "output": "from-responses"},
        ]},
    ]
    out = recover_tool_results_from_echo(tools, models)
    assert [_served(r) for r in out] == ["from-openai", "from-anthropic", "from-responses"]


def test_a_tool_message_envelope_is_recovered_from_its_content_and_status() -> None:
    envelope = {"content": "epoch,loss\n1,2.3\n", "status": "error", "name": "read_file",
                "artifact": None}
    tools = [_tool_record(envelope, name="read_file", **{
        "io.novafabric.langchain_result_kind": "tool_message",
    })]
    models = [_bedrock("tooluse_A", "epoch,loss\n1,2.3\n", status="error")]
    (out,) = recover_tool_results_from_echo(tools, models)
    assert _served(out) == envelope


# ── never served on anything but an exact digest match ───────────────────────


def test_a_digest_mismatch_leaves_the_record_unservable() -> None:
    tools = [_tool_record("the real result")]
    models = [_bedrock("tooluse_A", "a different text")]
    (out,) = recover_tool_results_from_echo(tools, models)
    assert out == tools[0]
    assert not_servable_reason(out) == PAYLOADS_OFF_REASON


def test_a_missing_digest_leaves_the_record_unservable() -> None:
    record = _tool_record("x")
    del record["extensions"]["io.novafabric.result_digest"]
    (out,) = recover_tool_results_from_echo([record], [_bedrock("tooluse_A", "x")])
    assert out == record


def test_no_echo_leaves_the_record_unservable() -> None:
    record = _tool_record("x")
    (out,) = recover_tool_results_from_echo([record], [_bedrock("tooluse_OTHER", "x")])
    assert out == record


@pytest.mark.parametrize("mutate", [
    # another reason besides payloads-off: the value would still be unservable
    lambda r: r["extensions"].__setitem__(
        "io.novafabric.not_servable_reason",
        f"{PAYLOADS_OFF_REASON}; 1 model/tool record(s) were written inside this call",
    ),
    lambda r: r.__setitem__("status", "error"),  # a recorded failure is never served
    lambda r: r["extensions"].pop(LC_ID),  # no model-visible call id to pair by
])
def test_records_that_are_unservable_for_another_reason_are_left_alone(mutate: Any) -> None:
    record = _tool_record("x")
    mutate(record)
    before = copy.deepcopy(record)
    (out,) = recover_tool_results_from_echo([record], [_bedrock("tooluse_A", "x")])
    assert out == before


def test_an_ambiguous_call_id_is_not_recovered() -> None:
    tools = [_tool_record("x"), _tool_record("x")]
    out = recover_tool_results_from_echo(tools, [_bedrock("tooluse_A", "x")])
    assert out == tools


def test_a_servable_record_is_returned_unchanged() -> None:
    record = _tool_record("x")
    record["extensions"]["io.novafabric.result_codec"] = "json-v1"
    record["extensions"].pop("io.novafabric.not_servable_reason")
    record["result"] = {"value": "x"}
    (out,) = recover_tool_results_from_echo([record], [_bedrock("tooluse_A", "y")])
    assert out is record
