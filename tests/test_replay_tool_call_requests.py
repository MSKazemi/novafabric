"""A function-calling agent must see its own tool-call requests during mocked replay.

Before this, neither half existed: the capture hooks recorded only the text of an
assistant turn (OpenAI ``message.tool_calls`` and Anthropic ``tool_use`` blocks were
never written), and the replay mock rebuilt only ``role``/``content``. A recorded
tool-calling turn therefore replayed as a text-only reply, so the agent diverged at
its first tool turn -- before tool-response substitution could even matter.
The recorded shape is the existing ``Message.tool_calls`` / ``ToolCallRef`` in
model-call.schema.json (``{id, name, arguments: object}``).
"""

from __future__ import annotations

import json
import types
from pathlib import Path
from unittest.mock import MagicMock

import jsonschema
import pytest

from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.hooks._anthropic import AnthropicHook
from novafabric.capture.hooks._openai import OpenAIHook
from novafabric.replay._dispatcher import (
    MockModelDispatcher,
    _mock_anthropic_response,
    _mock_openai_response,
)

_SCHEMA = json.loads(
    (Path(__file__).parents[1] / "src/novafabric/schemas/model-call.schema.json").read_text()
)


def _writer(tmp_path: Path) -> CapsuleWriter:
    w = CapsuleWriter(run_id="01HXAY7M5JZ8R7K4P9DPBYK2WX", base_dir=tmp_path)
    w.open()
    return w


def _records(w: CapsuleWriter) -> list[dict]:
    lines = (w.capsule_dir / "model-calls.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _choice_schema() -> dict:
    return {"$defs": _SCHEMA["$defs"], "$ref": "#/$defs/Choice"}


# ── capture ──────────────────────────────────────────────────────────────────


def test_openai_hook_records_tool_call_requests(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    hook = OpenAIHook(writer=w, parent_span_id="0" * 16)
    call = types.SimpleNamespace(
        id="call_1",
        type="function",
        function=types.SimpleNamespace(name="get_weather", arguments='{"city": "Paris"}'),
    )
    response = MagicMock(model="gpt-4o", id="chatcmpl-1")
    response.choices = [
        MagicMock(
            index=0,
            message=types.SimpleNamespace(role="assistant", content=None, tool_calls=[call]),
            finish_reason="tool_calls",
        )
    ]
    response.usage = MagicMock(prompt_tokens=3, completion_tokens=4)

    hook._intercept(MagicMock(return_value=response), model="gpt-4o", messages=[])

    (choice,) = _records(w)[0]["gen_ai.response.choices"]
    assert choice["message"]["tool_calls"] == [
        {"id": "call_1", "name": "get_weather", "arguments": {"city": "Paris"}}
    ]
    jsonschema.validate(choice, _choice_schema())


def test_openai_hook_keeps_unparseable_arguments_verbatim(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    hook = OpenAIHook(writer=w, parent_span_id="0" * 16)
    call = types.SimpleNamespace(
        id="call_1", type="function",
        function=types.SimpleNamespace(name="f", arguments='{"city": "Par'),
    )
    response = MagicMock(model="gpt-4o", id="x")
    response.choices = [MagicMock(
        index=0,
        message=types.SimpleNamespace(role="assistant", content=None, tool_calls=[call]),
        finish_reason="tool_calls",
    )]
    response.usage = MagicMock(prompt_tokens=0, completion_tokens=0)

    hook._intercept(MagicMock(return_value=response), model="gpt-4o", messages=[])

    (choice,) = _records(w)[0]["gen_ai.response.choices"]
    assert choice["message"]["tool_calls"][0]["arguments"] == {"_unparsed": '{"city": "Par'}


def test_openai_text_only_turn_has_no_tool_calls_key(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    hook = OpenAIHook(writer=w, parent_span_id="0" * 16)
    response = MagicMock(model="gpt-4o", id="x")
    response.choices = [MagicMock(index=0, message=MagicMock(content="hi", role="assistant"),
                                  finish_reason="stop")]
    response.usage = MagicMock(prompt_tokens=0, completion_tokens=0)

    hook._intercept(MagicMock(return_value=response), model="gpt-4o", messages=[])

    assert "tool_calls" not in _records(w)[0]["gen_ai.response.choices"][0]["message"]


def test_anthropic_hook_records_tool_use_blocks(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    hook = AnthropicHook(writer=w, parent_span_id="0" * 16)
    response = types.SimpleNamespace(
        model="claude-x",
        id="msg_1",
        stop_reason="tool_use",
        content=[
            types.SimpleNamespace(type="text", text="Let me check."),
            types.SimpleNamespace(type="tool_use", id="toolu_1", name="get_weather",
                                  input={"city": "Paris"}),
        ],
        usage=types.SimpleNamespace(input_tokens=1, output_tokens=2),
    )

    hook._intercept(MagicMock(return_value=response), model="claude-x", messages=[])

    (choice,) = _records(w)[0]["gen_ai.response.choices"]
    assert choice["message"]["content"] == "Let me check."
    assert choice["message"]["tool_calls"] == [
        {"id": "toolu_1", "name": "get_weather", "arguments": {"city": "Paris"}}
    ]


# ── replay ───────────────────────────────────────────────────────────────────


def _stored(finish: str) -> dict:
    return {
        "gen_ai.response.model": "m",
        "gen_ai.response.choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "name": "get_weather", "arguments": {"city": "Paris"}}
                ],
            },
            "finish_reason": finish,
        }],
    }


def test_openai_mock_serves_recorded_tool_calls() -> None:
    resp = _mock_openai_response(_stored("tool_calls"))
    (choice,) = resp.choices
    assert choice.finish_reason == "tool_calls"
    (tc,) = choice.message.tool_calls
    assert (tc.id, tc.type, tc.function.name) == ("call_1", "function", "get_weather")
    assert json.loads(tc.function.arguments) == {"city": "Paris"}


def test_openai_mock_text_turn_has_tool_calls_none() -> None:
    resp = _mock_openai_response({"gen_ai.response.choices": [
        {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
    ]})
    assert resp.choices[0].message.tool_calls is None
    assert resp.choices[0].message.content == "hi"


def test_openai_mock_unparsed_arguments_round_trip_verbatim() -> None:
    stored = _stored("tool_calls")
    stored["gen_ai.response.choices"][0]["message"]["tool_calls"][0]["arguments"] = {
        "_unparsed": '{"city": "Par'
    }
    tc = _mock_openai_response(stored).choices[0].message.tool_calls[0]
    assert tc.function.arguments == '{"city": "Par'


def test_anthropic_mock_serves_tool_use_blocks() -> None:
    stored = _stored("tool_use")
    stored["gen_ai.response.choices"][0]["message"]["content"] = "Let me check."
    resp = _mock_anthropic_response(stored)
    assert resp.stop_reason == "tool_use"
    assert [b.type for b in resp.content] == ["text", "tool_use"]
    block = resp.content[1]
    assert (block.id, block.name, block.input) == ("call_1", "get_weather", {"city": "Paris"})


def test_anthropic_mock_maps_openai_style_finish_reason() -> None:
    # Older/foreign records may carry the OTel enum value; Anthropic clients expect
    # their own stop_reason vocabulary.
    resp = _mock_anthropic_response(_stored("tool_calls"))
    assert resp.stop_reason == "tool_use"


def test_exhausted_queue_warns_instead_of_silently_serving_blanks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    pytest.importorskip("openai")
    import openai.resources.chat.completions as mod

    d = MockModelDispatcher([])
    d.install()
    try:
        mod.Completions.create(MagicMock(), model="gpt-4o", messages=[])
    finally:
        d.uninstall()
    assert "no recorded response left" in capsys.readouterr().err
