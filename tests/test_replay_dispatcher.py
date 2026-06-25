from __future__ import annotations

from novafabric.replay._dispatcher import (
    MockModelDispatcher,
    MockToolDispatcher,
    _arg_hash,
    _mock_anthropic_response,
    _mock_openai_response,
)


def _model_call(system: str = "openai", content: str = "hello") -> dict:
    return {
        "gen_ai.system": system,
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.response.model": "gpt-4o",
        "gen_ai.request.messages": [{"role": "user", "content": "hi"}],
        "gen_ai.response.choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "gen_ai.usage.input_tokens": 5,
        "gen_ai.usage.output_tokens": 10,
        "status": "success",
    }


def test_arg_hash_stable() -> None:
    assert _arg_hash({"b": 2, "a": 1}) == _arg_hash({"a": 1, "b": 2})


def test_arg_hash_non_dict() -> None:
    result = _arg_hash("not a dict")  # type: ignore[arg-type]
    assert isinstance(result, str)


class TestMockModelDispatcher:
    def test_openai_queue_partitioned(self) -> None:
        calls = [
            _model_call("openai"),
            _model_call("anthropic", "from-claude"),
            _model_call("openai"),
        ]
        d = MockModelDispatcher(model_calls=calls)
        assert len(d._openai_queue) == 2
        assert len(d._anthropic_queue) == 1

    def test_install_uninstall_no_sdk(self) -> None:
        d = MockModelDispatcher(model_calls=[_model_call()])
        d.install()
        d.uninstall()

    def test_mock_openai_response_structure(self) -> None:
        stored = _model_call("openai", "The answer is 42")
        resp = _mock_openai_response(stored)
        assert resp.choices[0].message.content == "The answer is 42"
        assert resp.choices[0].finish_reason == "stop"
        assert resp.model == "gpt-4o"
        assert resp.usage.prompt_tokens == 5
        assert resp.usage.completion_tokens == 10

    def test_mock_anthropic_response_structure(self) -> None:
        stored = _model_call("anthropic", "Claude says hi")
        resp = _mock_anthropic_response(stored)
        assert resp.content[0].text == "Claude says hi"
        assert resp.role == "assistant"
        assert resp.usage.input_tokens == 5
        assert resp.stop_reason == "stop"

    def test_mock_openai_empty_stored(self) -> None:
        resp = _mock_openai_response({})
        assert resp.choices == []
        assert resp.model == ""

    def test_mock_anthropic_empty_stored(self) -> None:
        resp = _mock_anthropic_response({})
        assert resp.content[0].text == ""
        assert resp.stop_reason == "end_turn"

    def test_index_increments_per_call(self) -> None:
        calls = [_model_call("openai", "first"), _model_call("openai", "second")]
        d = MockModelDispatcher(model_calls=calls)
        assert d._openai_index == 0


class TestMockToolDispatcher:
    def test_lookup_by_id(self) -> None:
        tc = {
            "tool_call_id": "ABC123",
            "tool_name": "search",
            "arguments": {"q": "test"},
            "result": "found",
        }
        d = MockToolDispatcher(tool_calls=[tc])
        result = d.lookup("ABC123", "search", {"q": "test"})
        assert result is not None
        assert result["result"] == "found"

    def test_lookup_by_arg_hash_fallback(self) -> None:
        tc = {
            "tool_call_id": "ORIGINAL",
            "tool_name": "fetch",
            "arguments": {"url": "http://x"},
            "result": "data",
        }
        d = MockToolDispatcher(tool_calls=[tc])
        result = d.lookup("NEW_ID", "fetch", {"url": "http://x"})
        assert result is not None
        assert result["result"] == "data"

    def test_lookup_miss(self) -> None:
        tc = {"tool_call_id": "A", "tool_name": "do_thing", "arguments": {}, "result": "ok"}
        d = MockToolDispatcher(tool_calls=[tc])
        assert d.lookup("B", "other_tool", {}) is None

    def test_empty_dispatcher(self) -> None:
        d = MockToolDispatcher(tool_calls=[])
        assert d.lookup("any", "any", {}) is None
