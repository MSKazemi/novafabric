import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from novafabric.capture.capsule import CapsuleWriter


@pytest.fixture(autouse=True)
def _clean_global_hook_state() -> None:
    """Reset the process-global hook installer + recorder before every test.

    The hook installer and current-recorder are process-global (a documented
    v0.1 limitation); a test elsewhere in the same xdist worker that leaves a
    recorder or installed hooks behind flips the semantics of install_all()
    here. Start each test from the same clean slate serial runs see.
    """
    from novafabric.capture.event_recorder import set_current_recorder
    from novafabric.capture.hooks import uninstall_all

    uninstall_all()
    set_current_recorder(None)

RUN_ID = "01HXTEST000000000000000000"


def make_writer(tmp_path: Path) -> CapsuleWriter:
    w = CapsuleWriter(run_id=RUN_ID, base_dir=tmp_path)
    w.open()
    return w


def _model_calls(tmp_path: Path) -> list[dict]:  # type: ignore[type-arg]
    text = (tmp_path / RUN_ID / "model-calls.jsonl").read_text().strip()
    return [json.loads(line) for line in text.splitlines() if line]


# ── OpenAI hook ──────────────────────────────────────────────────────────────

def test_openai_hook_records_chat(tmp_path: Path) -> None:
    from novafabric.capture.hooks._openai import OpenAIHook

    writer = make_writer(tmp_path)
    hook = OpenAIHook(writer=writer, parent_span_id="aabbccddeeff0011")

    fake_response = MagicMock()
    fake_response.model = "gpt-4o"
    fake_response.id = "chatcmpl-abc123"
    fake_response.choices = [MagicMock(
        index=0,
        message=MagicMock(content="Hello!", role="assistant"),
        finish_reason="stop",
    )]
    fake_response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)

    hook._intercept(MagicMock(return_value=fake_response), model="gpt-4o",
                    messages=[{"role": "user", "content": "Hi"}])

    records = _model_calls(tmp_path)
    assert len(records) == 1
    r = records[0]
    assert r["gen_ai.system"] == "openai"
    assert r["gen_ai.request.model"] == "gpt-4o"
    assert r["status"] == "success"
    assert r["gen_ai.usage.input_tokens"] == 10
    assert r["gen_ai.usage.output_tokens"] == 5


def test_openai_hook_extracts_request_semconv_fields(tmp_path: Path) -> None:
    """C-4 phase 2: per-SDK hook must populate the request-side semconv
    fields (temperature, max_tokens, top_p, seed, etc.) — not just
    model + messages. Without this, replay determinism is lost."""
    from novafabric.capture.hooks._openai import OpenAIHook

    writer = make_writer(tmp_path)
    hook = OpenAIHook(writer=writer, parent_span_id="0" * 16)

    fake_response = MagicMock(model="gpt-4o", id="chatcmpl-x",
                              choices=[], usage=MagicMock(prompt_tokens=0, completion_tokens=0))

    hook._intercept(
        MagicMock(return_value=fake_response),
        model="gpt-4o", messages=[],
        temperature=0.5, max_tokens=512, top_p=0.95,
        frequency_penalty=0.1, presence_penalty=0.2, seed=42,
        n=1, stop=["END"],
    )
    r = _model_calls(tmp_path)[0]
    assert r["gen_ai.request.temperature"] == 0.5
    assert r["gen_ai.request.max_tokens"] == 512
    assert r["gen_ai.request.top_p"] == 0.95
    assert r["gen_ai.request.frequency_penalty"] == 0.1
    assert r["gen_ai.request.presence_penalty"] == 0.2
    assert r["gen_ai.request.seed"] == 42
    assert r["gen_ai.request.choice.count"] == 1
    assert r["gen_ai.request.stop_sequences"] == ["END"]


def test_openai_hook_populates_response_id_and_finish_reasons(tmp_path: Path) -> None:
    """C-4 phase 2: response.id (for cross-referencing with provider logs)
    and finish_reasons (per-choice finish reason array) are part of the
    OTel-mandated record shape. The SDK gives us these in the response
    object — no excuse for not extracting them."""
    from novafabric.capture.hooks._openai import OpenAIHook

    writer = make_writer(tmp_path)
    hook = OpenAIHook(writer=writer, parent_span_id="0" * 16)

    fake_response = MagicMock()
    fake_response.model = "gpt-4o"
    fake_response.id = "chatcmpl-real-id-12345"
    fake_response.choices = [
        MagicMock(index=0, message=MagicMock(content="A", role="assistant"),
                  finish_reason="stop"),
        MagicMock(index=1, message=MagicMock(content="B", role="assistant"),
                  finish_reason="tool_calls"),
    ]
    fake_response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)

    hook._intercept(MagicMock(return_value=fake_response),
                    model="gpt-4o", messages=[], n=2)
    r = _model_calls(tmp_path)[0]
    assert r["gen_ai.response.id"] == "chatcmpl-real-id-12345"
    assert r["gen_ai.response.finish_reasons"] == ["stop", "tool_calls"]


def test_openai_hook_records_error(tmp_path: Path) -> None:
    from novafabric.capture.hooks._openai import OpenAIHook

    writer = make_writer(tmp_path)
    hook = OpenAIHook(writer=writer, parent_span_id="aabbccddeeff0011")

    def raising(**kwargs: object) -> None:
        raise ValueError("rate limit")

    with pytest.raises(ValueError):
        hook._intercept(raising, model="gpt-4o", messages=[])

    records = _model_calls(tmp_path)
    assert records[0]["status"] == "error"
    assert records[0]["error"]["type"] == "ValueError"


def test_openai_hook_install_noop_without_sdk(tmp_path: Path) -> None:
    from novafabric.capture.hooks._openai import OpenAIHook
    writer = make_writer(tmp_path)
    hook = OpenAIHook(writer=writer, parent_span_id="0" * 16)
    # Should not raise even if openai not importable
    with patch.dict("sys.modules", {"openai": None, "openai.resources": None,
                                    "openai.resources.chat": None,
                                    "openai.resources.chat.completions": None}):
        hook.install()  # must not raise


# ── Anthropic hook ────────────────────────────────────────────────────────────

def test_anthropic_hook_records_message(tmp_path: Path) -> None:
    from novafabric.capture.hooks._anthropic import AnthropicHook

    writer = make_writer(tmp_path)
    hook = AnthropicHook(writer=writer, parent_span_id="aabbccddeeff0011")

    fake_response = MagicMock()
    fake_response.model = "claude-sonnet-4-6"
    fake_response.id = "msg_abc123"
    fake_response.content = [MagicMock(text="Hello!", spec=["text"])]
    fake_response.usage = MagicMock(input_tokens=10, output_tokens=5)
    fake_response.stop_reason = "end_turn"

    hook._intercept(MagicMock(return_value=fake_response),
                    model="claude-sonnet-4-6",
                    messages=[{"role": "user", "content": "Hi"}],
                    max_tokens=100)

    records = _model_calls(tmp_path)
    assert len(records) == 1
    r = records[0]
    assert r["gen_ai.system"] == "anthropic"
    assert r["status"] == "success"
    assert r["gen_ai.usage.input_tokens"] == 10
    # max_tokens IS in the request kwargs so should appear in the record now (C-4 p2).
    assert r["gen_ai.request.max_tokens"] == 100


def test_anthropic_hook_extracts_request_semconv_fields(tmp_path: Path) -> None:
    """C-4 phase 2: same coverage as the OpenAI hook test, for Anthropic.
    Anthropic uses 'stop_sequences' (list) where OpenAI uses 'stop'."""
    from novafabric.capture.hooks._anthropic import AnthropicHook

    writer = make_writer(tmp_path)
    hook = AnthropicHook(writer=writer, parent_span_id="0" * 16)

    fake_response = MagicMock(
        model="claude-sonnet-4-6", id="msg_x",
        content=[], stop_reason="end_turn",
        usage=MagicMock(input_tokens=0, output_tokens=0),
    )
    hook._intercept(
        MagicMock(return_value=fake_response),
        model="claude-sonnet-4-6", messages=[], max_tokens=1024,
        temperature=0.7, top_p=0.95, top_k=40,
        stop_sequences=["END_OF_RESPONSE"],
    )
    r = _model_calls(tmp_path)[0]
    assert r["gen_ai.request.temperature"] == 0.7
    assert r["gen_ai.request.max_tokens"] == 1024
    assert r["gen_ai.request.top_p"] == 0.95
    assert r["gen_ai.request.top_k"] == 40
    assert r["gen_ai.request.stop_sequences"] == ["END_OF_RESPONSE"]


def test_anthropic_hook_populates_response_id_and_finish_reasons(tmp_path: Path) -> None:
    from novafabric.capture.hooks._anthropic import AnthropicHook

    writer = make_writer(tmp_path)
    hook = AnthropicHook(writer=writer, parent_span_id="0" * 16)

    fake_response = MagicMock()
    fake_response.model = "claude-sonnet-4-6"
    fake_response.id = "msg_real_id_xyz"
    fake_response.content = [MagicMock(text="Hello!", spec=["text"])]
    fake_response.usage = MagicMock(input_tokens=10, output_tokens=5)
    fake_response.stop_reason = "tool_use"

    hook._intercept(MagicMock(return_value=fake_response),
                    model="claude-sonnet-4-6", messages=[], max_tokens=100)
    r = _model_calls(tmp_path)[0]
    assert r["gen_ai.response.id"] == "msg_real_id_xyz"
    assert r["gen_ai.response.finish_reasons"] == ["tool_use"]


def test_anthropic_hook_records_error(tmp_path: Path) -> None:
    from novafabric.capture.hooks._anthropic import AnthropicHook
    writer = make_writer(tmp_path)
    hook = AnthropicHook(writer=writer, parent_span_id="0" * 16)

    def raising(**kwargs: object) -> None:
        raise ConnectionError("unreachable")

    with pytest.raises(ConnectionError):
        hook._intercept(raising, model="claude-haiku-4-5", messages=[], max_tokens=10)

    records = _model_calls(tmp_path)
    assert records[0]["status"] == "error"


# ── httpx hook ────────────────────────────────────────────────────────────────

def test_httpx_hook_skips_non_ai_urls(tmp_path: Path) -> None:
    from novafabric.capture.hooks._httpx import HttpxHook

    writer = make_writer(tmp_path)
    hook = HttpxHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url.__str__ = MagicMock(return_value="https://example.com/api")
    fake_resp = MagicMock(status_code=200)
    original = MagicMock(return_value=fake_resp)

    result = hook._wrapped_send(req, original=original)
    assert result is fake_resp
    assert _model_calls(tmp_path) == []


def test_httpx_hook_captures_openai_url(tmp_path: Path) -> None:
    from novafabric.capture.hooks._httpx import HttpxHook

    writer = make_writer(tmp_path)
    hook = HttpxHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url.__str__ = MagicMock(return_value="https://api.openai.com/v1/chat/completions")
    req.content = b'{"model":"gpt-4o","messages":[]}'
    fake_resp = MagicMock(status_code=200)
    original = MagicMock(return_value=fake_resp)

    hook._wrapped_send(req, original=original)
    records = _model_calls(tmp_path)
    assert len(records) == 1
    assert records[0]["gen_ai.system"] == "openai"


def test_httpx_hook_captures_anthropic_url(tmp_path: Path) -> None:
    from novafabric.capture.hooks._httpx import HttpxHook

    writer = make_writer(tmp_path)
    hook = HttpxHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url.__str__ = MagicMock(return_value="https://api.anthropic.com/v1/messages")
    req.content = b'{"model":"claude-sonnet-4-6","messages":[]}'
    fake_resp = MagicMock(status_code=200)

    hook._wrapped_send(req, original=MagicMock(return_value=fake_resp))
    records = _model_calls(tmp_path)
    assert records[0]["gen_ai.system"] == "anthropic"


def test_httpx_hook_records_error_status(tmp_path: Path) -> None:
    from novafabric.capture.hooks._httpx import HttpxHook

    writer = make_writer(tmp_path)
    hook = HttpxHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url.__str__ = MagicMock(return_value="https://api.openai.com/v1/chat/completions")
    req.content = b'{"model":"gpt-4o","messages":[]}'
    fake_resp = MagicMock(status_code=429)

    hook._wrapped_send(req, original=MagicMock(return_value=fake_resp))
    records = _model_calls(tmp_path)
    assert records[0]["status"] == "error"


def test_httpx_hook_records_attempt_when_call_raises(tmp_path: Path) -> None:
    """Connection failures must still produce a record (status=error).
    Regression test for v0.6.0 cleanup item: prior to this fix, _httpx.py
    silently dropped records when the underlying call raised."""
    from novafabric.capture.hooks._httpx import HttpxHook

    writer = make_writer(tmp_path)
    hook = HttpxHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url.__str__ = MagicMock(
        return_value="https://api.openai.com/v1/chat/completions"
    )
    req.content = b'{"model":"gpt-4o","messages":[]}'

    def boom(request, **kwargs):  # type: ignore[no-untyped-def]
        raise ConnectionError("upstream unreachable")

    with pytest.raises(ConnectionError):
        hook._wrapped_send(req, original=boom)
    records = _model_calls(tmp_path)
    assert len(records) == 1
    assert records[0]["status"] == "error"
    # Original request context preserved even though the call failed.
    assert records[0]["gen_ai.request.model"] == "gpt-4o"


# ── httpx async hook (AsyncClient.send) ───────────────────────────────────────


def _run_async(coro):  # type: ignore[no-untyped-def]
    import asyncio
    return asyncio.run(coro)


def test_httpx_async_hook_skips_non_ai_urls(tmp_path: Path) -> None:
    from novafabric.capture.hooks._httpx import HttpxHook

    writer = make_writer(tmp_path)
    hook = HttpxHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url.__str__ = MagicMock(return_value="https://example.com/api")
    fake_resp = MagicMock(status_code=200)

    async def original(request, **kwargs):  # type: ignore[no-untyped-def]
        return fake_resp

    result = _run_async(hook._wrapped_send_async(req, original=original))
    assert result is fake_resp
    assert _model_calls(tmp_path) == []


def test_httpx_async_hook_captures_openai_url(tmp_path: Path) -> None:
    from novafabric.capture.hooks._httpx import HttpxHook

    writer = make_writer(tmp_path)
    hook = HttpxHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url.__str__ = MagicMock(return_value="https://api.openai.com/v1/chat/completions")
    req.content = b'{"model":"gpt-4o","messages":[]}'
    fake_resp = MagicMock(status_code=200)

    async def original(request, **kwargs):  # type: ignore[no-untyped-def]
        return fake_resp

    _run_async(hook._wrapped_send_async(req, original=original))
    records = _model_calls(tmp_path)
    assert len(records) == 1
    assert records[0]["gen_ai.system"] == "openai"
    assert records[0]["gen_ai.request.model"] == "gpt-4o"
    assert records[0]["status"] == "success"


def test_httpx_async_hook_captures_ollama_url(tmp_path: Path) -> None:
    from novafabric.capture.hooks._httpx import HttpxHook

    writer = make_writer(tmp_path)
    hook = HttpxHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url.__str__ = MagicMock(return_value="http://localhost:11434/api/chat")
    req.content = b'{"model":"qwen3:35b","messages":[]}'
    fake_resp = MagicMock(status_code=200)

    async def original(request, **kwargs):  # type: ignore[no-untyped-def]
        return fake_resp

    _run_async(hook._wrapped_send_async(req, original=original))
    records = _model_calls(tmp_path)
    assert len(records) == 1
    assert records[0]["gen_ai.system"] == "ollama"


def test_httpx_hook_captures_ollama_non_default_port_via_env(tmp_path: Path) -> None:
    """OLLAMA_BASE_URL with a non-default port must be captured at call time.

    sitecustomize installs the hook before python-dotenv loads the env var, so
    the env var is not available at hook-install time.  The URL registry must
    check OLLAMA_BASE_URL dynamically on every request.
    """
    from novafabric.capture.hooks._httpx import HttpxHook

    writer = make_writer(tmp_path)
    hook = HttpxHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url.__str__ = MagicMock(return_value="http://localhost:11437/api/chat")
    req.content = b'{"model":"mistral-nemo:latest","messages":[]}'
    fake_resp = MagicMock(status_code=200)

    with patch.dict("os.environ", {"OLLAMA_BASE_URL": "http://localhost:11437"}):
        hook._wrapped_send(req, original=MagicMock(return_value=fake_resp))

    records = _model_calls(tmp_path)
    assert len(records) == 1
    assert records[0]["gen_ai.system"] == "ollama"


def test_httpx_async_hook_captures_ollama_non_default_port_via_env(tmp_path: Path) -> None:
    from novafabric.capture.hooks._httpx import HttpxHook

    writer = make_writer(tmp_path)
    hook = HttpxHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url.__str__ = MagicMock(return_value="http://localhost:11436/api/chat")
    req.content = b'{"model":"ministral-3:14b","messages":[]}'
    fake_resp = MagicMock(status_code=200)

    async def original(request, **kwargs):  # type: ignore[no-untyped-def]
        return fake_resp

    with patch.dict("os.environ", {"OLLAMA_BASE_URL": "http://localhost:11436"}):
        _run_async(hook._wrapped_send_async(req, original=original))

    records = _model_calls(tmp_path)
    assert len(records) == 1
    assert records[0]["gen_ai.system"] == "ollama"


def test_httpx_hook_captures_ollama_via_ollama_host_env(tmp_path: Path) -> None:
    """OLLAMA_HOST (raw ollama SDK) must also trigger capture."""
    from novafabric.capture.hooks._httpx import HttpxHook

    writer = make_writer(tmp_path)
    hook = HttpxHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url.__str__ = MagicMock(return_value="http://localhost:11437/api/chat")
    req.content = b'{"model":"qwen3:35b","messages":[]}'
    fake_resp = MagicMock(status_code=200)

    with patch.dict("os.environ", {"OLLAMA_HOST": "localhost:11437"}):
        hook._wrapped_send(req, original=MagicMock(return_value=fake_resp))

    records = _model_calls(tmp_path)
    assert len(records) == 1
    assert records[0]["gen_ai.system"] == "ollama"


def test_url_registry_env_ollama_match_not_set(tmp_path: Path) -> None:
    """Without OLLAMA_BASE_URL/OLLAMA_HOST, non-default port is not captured."""
    from novafabric.capture.hooks._httpx import HttpxHook

    writer = make_writer(tmp_path)
    hook = HttpxHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url.__str__ = MagicMock(return_value="http://localhost:11437/api/chat")
    fake_resp = MagicMock(status_code=200)

    env_without_ollama = {k: v for k, v in __import__("os").environ.items()
                          if k not in ("OLLAMA_BASE_URL", "OLLAMA_HOST")}
    with patch.dict("os.environ", env_without_ollama, clear=True):
        result = hook._wrapped_send(req, original=MagicMock(return_value=fake_resp))

    assert result is fake_resp
    assert _model_calls(tmp_path) == []


def test_httpx_async_hook_records_attempt_when_call_raises(tmp_path: Path) -> None:
    from novafabric.capture.hooks._httpx import HttpxHook

    writer = make_writer(tmp_path)
    hook = HttpxHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url.__str__ = MagicMock(return_value="https://api.openai.com/v1/chat/completions")
    req.content = b'{"model":"gpt-4o","messages":[]}'

    async def boom(request, **kwargs):  # type: ignore[no-untyped-def]
        raise ConnectionError("upstream unreachable")

    with pytest.raises(ConnectionError):
        _run_async(hook._wrapped_send_async(req, original=boom))
    records = _model_calls(tmp_path)
    assert len(records) == 1
    assert records[0]["status"] == "error"
    assert records[0]["gen_ai.request.model"] == "gpt-4o"


def test_httpx_hook_install_also_patches_async_client(tmp_path: Path) -> None:
    from novafabric.capture.hooks._httpx import HttpxHook

    writer = make_writer(tmp_path)
    hook = HttpxHook(writer=writer, parent_span_id="0" * 16)

    fake_httpx = MagicMock()
    original_sync = MagicMock()
    original_async = MagicMock()
    fake_httpx.Client = MagicMock()
    fake_httpx.Client.send = original_sync
    fake_httpx.AsyncClient = MagicMock()
    fake_httpx.AsyncClient.send = original_async

    with patch.dict("sys.modules", {"httpx": fake_httpx}):
        hook.install()
        assert hook._original_sync is original_sync
        assert hook._original_async is original_async
        hook.uninstall()
        assert hook._original_sync is None
        assert hook._original_async is None
        assert fake_httpx.Client.send is original_sync
        assert fake_httpx.AsyncClient.send is original_async


def test_httpx_hook_record_handles_invalid_json_body(tmp_path: Path) -> None:
    from novafabric.capture.hooks._httpx import HttpxHook

    writer = make_writer(tmp_path)
    hook = HttpxHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url.__str__ = MagicMock(return_value="https://api.openai.com/v1/chat/completions")
    req.content = b"not-json{{"
    fake_resp = MagicMock(status_code=200)

    hook._wrapped_send(req, original=MagicMock(return_value=fake_resp))
    records = _model_calls(tmp_path)
    assert len(records) == 1
    assert records[0]["gen_ai.system"] == "openai"


def test_httpx_hook_record_handles_non_dict_json_body(tmp_path: Path) -> None:
    from novafabric.capture.hooks._httpx import HttpxHook

    writer = make_writer(tmp_path)
    hook = HttpxHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url.__str__ = MagicMock(return_value="https://api.openai.com/v1/chat/completions")
    req.content = b'["list", "not", "dict"]'
    fake_resp = MagicMock(status_code=200)

    hook._wrapped_send(req, original=MagicMock(return_value=fake_resp))
    records = _model_calls(tmp_path)
    assert len(records) == 1


def test_httpx_hook_sync_raises_when_no_original_non_registry(tmp_path: Path) -> None:
    from novafabric.capture.hooks._httpx import HttpxHook

    writer = make_writer(tmp_path)
    hook = HttpxHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url.__str__ = MagicMock(return_value="https://example.com/api")

    with pytest.raises(RuntimeError, match="original send function required"):
        hook._wrapped_send(req, original=None)


def test_httpx_async_hook_raises_when_no_original_non_registry(tmp_path: Path) -> None:
    from novafabric.capture.hooks._httpx import HttpxHook

    writer = make_writer(tmp_path)
    hook = HttpxHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url.__str__ = MagicMock(return_value="https://example.com/api")

    with pytest.raises(RuntimeError, match="original send function required"):
        _run_async(hook._wrapped_send_async(req, original=None))


def test_httpx_hook_record_suppresses_writer_exception(tmp_path: Path) -> None:
    from novafabric.capture.hooks._httpx import HttpxHook

    writer = make_writer(tmp_path)
    writer.append_model_call = MagicMock(side_effect=OSError("disk full"))  # type: ignore[method-assign]
    hook = HttpxHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url.__str__ = MagicMock(return_value="https://api.openai.com/v1/chat/completions")
    req.content = b'{"model":"gpt-4o","messages":[]}'
    fake_resp = MagicMock(status_code=200)

    # Must not raise even if the writer fails.
    result = hook._wrapped_send(req, original=MagicMock(return_value=fake_resp))
    assert result is fake_resp


# ── requests hook ────────────────────────────────────────────────────────────

def test_requests_hook_skips_non_ai_urls(tmp_path: Path) -> None:
    from novafabric.capture.hooks._requests import RequestsHook

    writer = make_writer(tmp_path)
    hook = RequestsHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url = "https://example.com/api"
    fake_resp = MagicMock(status_code=200)
    original = MagicMock(return_value=fake_resp)

    result = hook._wrapped_send(req, original=original)
    assert result is fake_resp
    assert _model_calls(tmp_path) == []


def test_requests_hook_captures_openai_url(tmp_path: Path) -> None:
    from novafabric.capture.hooks._requests import RequestsHook

    writer = make_writer(tmp_path)
    hook = RequestsHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url = "https://api.openai.com/v1/chat/completions"
    req.body = b'{"model":"gpt-4o","messages":[{"role":"user","content":"hi"}]}'
    fake_resp = MagicMock(status_code=200)
    original = MagicMock(return_value=fake_resp)

    hook._wrapped_send(req, original=original)
    records = _model_calls(tmp_path)
    assert len(records) == 1
    assert records[0]["gen_ai.system"] == "openai"
    assert records[0]["gen_ai.request.model"] == "gpt-4o"


def test_requests_hook_records_error_status(tmp_path: Path) -> None:
    from novafabric.capture.hooks._requests import RequestsHook

    writer = make_writer(tmp_path)
    hook = RequestsHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url = "https://api.anthropic.com/v1/messages"
    req.body = b'{"model":"claude-sonnet-4-6","messages":[]}'
    fake_resp = MagicMock(status_code=503)

    hook._wrapped_send(req, original=MagicMock(return_value=fake_resp))
    records = _model_calls(tmp_path)
    assert records[0]["status"] == "error"


def test_requests_hook_records_attempt_when_call_raises(tmp_path: Path) -> None:
    """Connection failures must still produce a record (status=error).
    Regression test for v0.6.0 cleanup item: prior to this fix,
    _requests.py silently dropped records when the underlying call
    raised."""
    from novafabric.capture.hooks._requests import RequestsHook

    writer = make_writer(tmp_path)
    hook = RequestsHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url = "https://api.anthropic.com/v1/messages"
    req.body = b'{"model":"claude-sonnet-4-6","messages":[]}'

    def boom(prepared_request, **kwargs):  # type: ignore[no-untyped-def]
        raise ConnectionError("upstream unreachable")

    with pytest.raises(ConnectionError):
        hook._wrapped_send(req, original=boom)
    records = _model_calls(tmp_path)
    assert len(records) == 1
    assert records[0]["status"] == "error"
    assert records[0]["gen_ai.request.model"] == "claude-sonnet-4-6"


def test_requests_hook_install_uninstall_with_fake_sdk(tmp_path: Path) -> None:
    """Install + uninstall must leave Session.send byte-identical, with the
    SDK provided as a fake module (mirrors the OpenAI hook pattern — no real
    `requests` install needed in dev deps)."""
    import sys
    import types as _types

    from novafabric.capture.hooks._requests import RequestsHook

    original_send = MagicMock()

    class _FakeSession:
        send = original_send

    fake_requests = _types.SimpleNamespace(Session=_FakeSession)

    writer = make_writer(tmp_path)
    hook = RequestsHook(writer=writer, parent_span_id="0" * 16)

    with patch.dict(sys.modules, {"requests": fake_requests}):
        hook.install()
        assert _FakeSession.send is not original_send
        hook.uninstall()
        assert _FakeSession.send is original_send


def test_requests_hook_install_noop_without_sdk(tmp_path: Path) -> None:
    """Absent the requests SDK, install must be silent and uninstall a no-op."""
    import sys

    from novafabric.capture.hooks._requests import RequestsHook

    writer = make_writer(tmp_path)
    hook = RequestsHook(writer=writer, parent_span_id="0" * 16)
    with patch.dict(sys.modules, {"requests": None}):
        hook.install()  # must not raise
    hook.uninstall()  # idempotent


# ── install / uninstall coverage ─────────────────────────────────────────────

def test_openai_hook_install_uninstall_with_mock_sdk(tmp_path: Path) -> None:
    import types

    from novafabric.capture.hooks._openai import OpenAIHook

    writer = make_writer(tmp_path)
    hook = OpenAIHook(writer=writer, parent_span_id="0" * 16)

    fake_completions = MagicMock()
    fake_create = MagicMock()
    fake_completions.Completions = MagicMock()
    fake_completions.Completions.create = fake_create

    fake_chat = types.SimpleNamespace(completions=fake_completions)
    fake_resources = types.SimpleNamespace(chat=fake_chat)
    fake_openai = types.SimpleNamespace(resources=fake_resources)

    with patch.dict("sys.modules", {
        "openai": fake_openai,
        "openai.resources": fake_resources,
        "openai.resources.chat": fake_chat,
        "openai.resources.chat.completions": fake_completions,
    }):
        hook.install()
        assert hook._original is fake_create
        hook.uninstall()
        assert hook._original is None
        assert fake_completions.Completions.create is fake_create


def test_openai_hook_uninstall_noop_when_not_installed(tmp_path: Path) -> None:
    from novafabric.capture.hooks._openai import OpenAIHook

    writer = make_writer(tmp_path)
    hook = OpenAIHook(writer=writer, parent_span_id="0" * 16)
    hook.uninstall()  # should not raise


def test_anthropic_hook_install_uninstall_with_mock_sdk(tmp_path: Path) -> None:
    import types

    from novafabric.capture.hooks._anthropic import AnthropicHook

    writer = make_writer(tmp_path)
    hook = AnthropicHook(writer=writer, parent_span_id="0" * 16)

    fake_messages_mod = MagicMock()
    fake_create = MagicMock()
    fake_messages_mod.Messages = MagicMock()
    fake_messages_mod.Messages.create = fake_create

    # Full attribute chain needed: `import a.b.c as x` does `x = sys.modules["a"].b.c`
    fake_resources = types.SimpleNamespace(messages=fake_messages_mod)
    fake_anthropic = types.SimpleNamespace(resources=fake_resources)

    with patch.dict("sys.modules", {
        "anthropic": fake_anthropic,
        "anthropic.resources": fake_resources,
        "anthropic.resources.messages": fake_messages_mod,
    }):
        hook.install()
        assert hook._original is fake_create
        hook.uninstall()
        assert hook._original is None


def test_httpx_hook_install_uninstall_with_mock_sdk(tmp_path: Path) -> None:

    from novafabric.capture.hooks._httpx import HttpxHook

    writer = make_writer(tmp_path)
    hook = HttpxHook(writer=writer, parent_span_id="0" * 16)

    fake_client = MagicMock()
    original_send = MagicMock()
    original_async_send = MagicMock()
    fake_client.Client = MagicMock()
    fake_client.Client.send = original_send
    fake_client.AsyncClient = MagicMock()
    fake_client.AsyncClient.send = original_async_send

    with patch.dict("sys.modules", {"httpx": fake_client}):
        hook.install()
        assert hook._original_sync is original_send
        assert hook._original_async is original_async_send
        hook.uninstall()
        assert hook._original_sync is None
        assert hook._original_async is None
        assert fake_client.Client.send is original_send
        assert fake_client.AsyncClient.send is original_async_send


def test_httpx_hook_uninstall_noop_when_not_installed(tmp_path: Path) -> None:
    from novafabric.capture.hooks._httpx import HttpxHook

    writer = make_writer(tmp_path)
    hook = HttpxHook(writer=writer, parent_span_id="0" * 16)
    hook.uninstall()  # should not raise


def test_install_all_and_uninstall_all(tmp_path: Path) -> None:
    from novafabric.capture.hooks import _installed, install_all, uninstall_all

    writer = make_writer(tmp_path)
    install_all(writer=writer, parent_span_id="0" * 16)
    assert len(_installed) >= 1
    uninstall_all()
    assert _installed == []


# ── EventRecorder singleton wiring (network/file event capture fix) ──────────


def test_install_all_sets_event_recorder(tmp_path: Path) -> None:
    """Regression: install_all must set the EventRecorder singleton so wire-level
    hooks can write network_events.jsonl. Before this fix the recorder was only
    set by the orchestrator in the PARENT process, leaving it None in the child
    subprocess / SDK / adapter contexts where the hooks actually run — silently
    dropping all NetworkEvent/FileEvent records."""
    from novafabric.capture.event_recorder import (
        get_current_recorder,
        set_current_recorder,
    )
    from novafabric.capture.hooks import install_all, uninstall_all

    set_current_recorder(None)  # simulate fresh child process
    writer = make_writer(tmp_path)
    try:
        install_all(writer=writer, parent_span_id="0" * 16)
        rec = get_current_recorder()
        assert rec is not None, "install_all must set the recorder singleton"
        assert rec._capsule_dir == writer.capsule_dir
    finally:
        uninstall_all()
    # uninstall_all clears the recorder it set.
    assert get_current_recorder() is None


def test_install_all_does_not_clobber_existing_recorder(tmp_path: Path) -> None:
    """When an orchestrator already set a recorder, install_all must leave it
    alone (and uninstall_all must not clear it)."""
    from novafabric.capture.event_recorder import (
        EventRecorder,
        get_current_recorder,
        set_current_recorder,
    )
    from novafabric.capture.hooks import install_all, uninstall_all

    orchestrator_rec = EventRecorder(
        capsule_dir=tmp_path / "owned", run_id="OWNED", capsule_id="OWNED"
    )
    set_current_recorder(orchestrator_rec)
    writer = make_writer(tmp_path)
    try:
        install_all(writer=writer, parent_span_id="0" * 16)
        assert get_current_recorder() is orchestrator_rec
    finally:
        uninstall_all()
    # Externally-owned recorder survives uninstall_all.
    assert get_current_recorder() is orchestrator_rec
    set_current_recorder(None)  # cleanup


def test_httpx_hook_writes_network_event_via_recorder(tmp_path: Path) -> None:
    """End-to-end: after install_all sets the recorder, an httpx call to a
    non-AI URL still produces a network_events.jsonl record (NetworkEvent is
    recorded for ALL hosts, AI or not)."""
    from novafabric.capture.event_recorder import set_current_recorder
    from novafabric.capture.hooks import install_all, uninstall_all
    from novafabric.capture.hooks._httpx import HttpxHook

    set_current_recorder(None)
    writer = make_writer(tmp_path)
    install_all(writer=writer, parent_span_id="0" * 16)
    try:
        hook = HttpxHook(writer=writer, parent_span_id="0" * 16)
        req = MagicMock()
        req.url.__str__ = MagicMock(return_value="https://api.openai.com/v1/chat/completions")
        req.content = b'{"model":"gpt-4o","messages":[]}'
        req.method = "POST"
        fake_resp = MagicMock(status_code=200, content=b"{}")
        hook._wrapped_send(req, original=MagicMock(return_value=fake_resp))
    finally:
        uninstall_all()

    net_path = tmp_path / RUN_ID / "network_events.jsonl"
    assert net_path.exists(), "network_events.jsonl must be written"
    lines = [json.loads(line) for line in net_path.read_text().splitlines() if line]
    assert len(lines) == 1
    assert lines[0]["host"] == "api.openai.com"
    assert lines[0]["is_ai_api"] is True
    assert lines[0]["library"] == "httpx"


# ── aiohttp hook (RFC-0001 Option C, sub-track C-3.1) ────────────────────────


def test_aiohttp_hook_skips_non_ai_urls(tmp_path: Path) -> None:
    from novafabric.capture.hooks._aiohttp import AiohttpHook

    writer = make_writer(tmp_path)
    hook = AiohttpHook(writer=writer, parent_span_id="0" * 16)

    fake_resp = MagicMock(status=200)

    async def original(method, url, **kwargs):  # type: ignore[no-untyped-def]
        return fake_resp

    result = _run_async(
        hook._wrapped_request("GET", "https://example.com/api", original=original)
    )
    assert result is fake_resp
    assert _model_calls(tmp_path) == []


def test_aiohttp_hook_captures_openai_url(tmp_path: Path) -> None:
    from novafabric.capture.hooks._aiohttp import AiohttpHook

    writer = make_writer(tmp_path)
    hook = AiohttpHook(writer=writer, parent_span_id="0" * 16)

    fake_resp = MagicMock(status=200)

    async def original(method, url, **kwargs):  # type: ignore[no-untyped-def]
        return fake_resp

    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    _run_async(hook._wrapped_request(
        "POST", "https://api.openai.com/v1/chat/completions",
        original=original, json=body,
    ))

    records = _model_calls(tmp_path)
    assert len(records) == 1
    assert records[0]["gen_ai.system"] == "openai"
    assert records[0]["gen_ai.request.model"] == "gpt-4o"
    assert records[0]["status"] == "success"


def test_aiohttp_hook_captures_anthropic_url(tmp_path: Path) -> None:
    from novafabric.capture.hooks._aiohttp import AiohttpHook

    writer = make_writer(tmp_path)
    hook = AiohttpHook(writer=writer, parent_span_id="0" * 16)

    fake_resp = MagicMock(status=200)

    async def original(method, url, **kwargs):  # type: ignore[no-untyped-def]
        return fake_resp

    body = {"model": "claude-sonnet-4-6", "messages": []}
    _run_async(hook._wrapped_request(
        "POST", "https://api.anthropic.com/v1/messages",
        original=original, json=body,
    ))
    assert _model_calls(tmp_path)[0]["gen_ai.system"] == "anthropic"


def test_aiohttp_hook_records_error_status(tmp_path: Path) -> None:
    from novafabric.capture.hooks._aiohttp import AiohttpHook

    writer = make_writer(tmp_path)
    hook = AiohttpHook(writer=writer, parent_span_id="0" * 16)

    fake_resp = MagicMock(status=503)

    async def original(method, url, **kwargs):  # type: ignore[no-untyped-def]
        return fake_resp

    _run_async(hook._wrapped_request(
        "POST", "https://api.openai.com/v1/chat/completions",
        original=original,
        json={"model": "gpt-4o", "messages": []},
    ))
    assert _model_calls(tmp_path)[0]["status"] == "error"


def test_aiohttp_hook_records_attempt_when_call_raises(tmp_path: Path) -> None:
    """Connection failures must still produce a record (status=error). This
    is the analogue of the empty-response capture the OpenAI/Anthropic hooks
    already do; it's what makes failed runs replayable."""
    from novafabric.capture.hooks._aiohttp import AiohttpHook

    writer = make_writer(tmp_path)
    hook = AiohttpHook(writer=writer, parent_span_id="0" * 16)

    async def boom(method, url, **kwargs):  # type: ignore[no-untyped-def]
        raise ConnectionError("upstream unreachable")

    with pytest.raises(ConnectionError):
        _run_async(hook._wrapped_request(
            "POST", "https://api.openai.com/v1/chat/completions",
            original=boom,
            json={"model": "gpt-4o", "messages": []},
        ))
    records = _model_calls(tmp_path)
    assert len(records) == 1
    assert records[0]["status"] == "error"


def test_aiohttp_hook_install_uninstall_with_fake_sdk(tmp_path: Path) -> None:
    """Install + uninstall must leave ClientSession._request byte-identical,
    using a fake module so we don't need aiohttp in dev deps."""
    import sys
    import types as _types

    from novafabric.capture.hooks._aiohttp import AiohttpHook

    async def original_request(self, method, url, **kwargs):  # type: ignore[no-untyped-def]
        return MagicMock(status=200)

    class _FakeClientSession:
        _request = original_request

    fake_aiohttp = _types.SimpleNamespace(ClientSession=_FakeClientSession)

    writer = make_writer(tmp_path)
    hook = AiohttpHook(writer=writer, parent_span_id="0" * 16)

    with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
        hook.install()
        assert _FakeClientSession._request is not original_request
        hook.uninstall()
        assert _FakeClientSession._request is original_request


def test_aiohttp_hook_install_noop_without_sdk(tmp_path: Path) -> None:
    import sys

    from novafabric.capture.hooks._aiohttp import AiohttpHook

    writer = make_writer(tmp_path)
    hook = AiohttpHook(writer=writer, parent_span_id="0" * 16)
    with patch.dict(sys.modules, {"aiohttp": None}):
        hook.install()  # must not raise
    hook.uninstall()  # idempotent


def test_aiohttp_hook_extracts_body_from_data_string(tmp_path: Path) -> None:
    """When body comes as `data=<json-string>` instead of `json=<dict>`,
    the hook should still extract model/messages cleanly."""
    from novafabric.capture.hooks._aiohttp import AiohttpHook

    writer = make_writer(tmp_path)
    hook = AiohttpHook(writer=writer, parent_span_id="0" * 16)

    async def original(method, url, **kwargs):  # type: ignore[no-untyped-def]
        return MagicMock(status=200)

    body_str = json.dumps({"model": "gpt-4o", "messages": []})
    _run_async(hook._wrapped_request(
        "POST", "https://api.openai.com/v1/chat/completions",
        original=original, data=body_str,
    ))
    assert _model_calls(tmp_path)[0]["gen_ai.request.model"] == "gpt-4o"


# ── urllib3 hook (RFC-0001 Option C, sub-track C-3.2) ────────────────────────


def _fake_pool(host: str = "api.openai.com", scheme: str = "https") -> MagicMock:
    pool = MagicMock()
    pool.scheme = scheme
    pool.host = host
    pool.port = 443 if scheme == "https" else 80
    return pool


def test_urllib3_hook_skips_non_ai_urls(tmp_path: Path) -> None:
    from novafabric.capture.hooks._urllib3 import Urllib3Hook

    writer = make_writer(tmp_path)
    hook = Urllib3Hook(writer=writer, parent_span_id="0" * 16)

    pool = _fake_pool(host="example.com")
    fake_resp = MagicMock(status=200)
    original = MagicMock(return_value=fake_resp)

    result = hook._wrapped_urlopen(pool, "GET", "/api", original=original)
    assert result is fake_resp
    assert _model_calls(tmp_path) == []


def test_urllib3_hook_captures_openai_url(tmp_path: Path) -> None:
    from novafabric.capture.hooks._urllib3 import Urllib3Hook

    writer = make_writer(tmp_path)
    hook = Urllib3Hook(writer=writer, parent_span_id="0" * 16)

    pool = _fake_pool(host="api.openai.com")
    fake_resp = MagicMock(status=200)
    original = MagicMock(return_value=fake_resp)
    body = b'{"model":"gpt-4o","messages":[{"role":"user","content":"hi"}]}'

    hook._wrapped_urlopen(
        pool, "POST", "/v1/chat/completions",
        original=original, body=body,
    )
    records = _model_calls(tmp_path)
    assert len(records) == 1
    assert records[0]["gen_ai.system"] == "openai"
    assert records[0]["gen_ai.request.model"] == "gpt-4o"
    assert records[0]["endpoint"] == "https://api.openai.com/v1/chat/completions"


def test_urllib3_hook_records_error_status(tmp_path: Path) -> None:
    from novafabric.capture.hooks._urllib3 import Urllib3Hook

    writer = make_writer(tmp_path)
    hook = Urllib3Hook(writer=writer, parent_span_id="0" * 16)

    pool = _fake_pool(host="api.anthropic.com")
    fake_resp = MagicMock(status=503)
    original = MagicMock(return_value=fake_resp)

    hook._wrapped_urlopen(
        pool, "POST", "/v1/messages",
        original=original,
        body=b'{"model":"claude-sonnet-4-6","messages":[]}',
    )
    assert _model_calls(tmp_path)[0]["status"] == "error"


def test_urllib3_hook_records_attempt_when_call_raises(tmp_path: Path) -> None:
    from novafabric.capture.hooks._urllib3 import Urllib3Hook

    writer = make_writer(tmp_path)
    hook = Urllib3Hook(writer=writer, parent_span_id="0" * 16)

    pool = _fake_pool(host="api.openai.com")

    def boom(method, url, **kwargs):  # type: ignore[no-untyped-def]
        raise ConnectionError("upstream unreachable")

    with pytest.raises(ConnectionError):
        hook._wrapped_urlopen(
            pool, "POST", "/v1/chat/completions",
            original=boom,
            body=b'{"model":"gpt-4o","messages":[]}',
        )
    records = _model_calls(tmp_path)
    assert len(records) == 1
    assert records[0]["status"] == "error"


def test_urllib3_hook_install_uninstall_with_fake_sdk(tmp_path: Path) -> None:
    """Install + uninstall must leave HTTPConnectionPool.urlopen byte-identical,
    using a fake module so we don't need urllib3 in dev deps."""
    import sys
    import types as _types

    from novafabric.capture.hooks._urllib3 import Urllib3Hook

    original_urlopen = MagicMock()

    class _FakeHTTPConnectionPool:
        urlopen = original_urlopen

    fake_connectionpool = _types.SimpleNamespace(
        HTTPConnectionPool=_FakeHTTPConnectionPool
    )
    fake_urllib3 = _types.SimpleNamespace(connectionpool=fake_connectionpool)

    writer = make_writer(tmp_path)
    hook = Urllib3Hook(writer=writer, parent_span_id="0" * 16)

    with patch.dict(sys.modules, {
        "urllib3": fake_urllib3,
        "urllib3.connectionpool": fake_connectionpool,
    }):
        hook.install()
        assert _FakeHTTPConnectionPool.urlopen is not original_urlopen
        hook.uninstall()
        assert _FakeHTTPConnectionPool.urlopen is original_urlopen


def test_urllib3_hook_install_noop_without_sdk(tmp_path: Path) -> None:
    import sys

    from novafabric.capture.hooks._urllib3 import Urllib3Hook

    writer = make_writer(tmp_path)
    hook = Urllib3Hook(writer=writer, parent_span_id="0" * 16)
    with patch.dict(sys.modules, {
        "urllib3": None, "urllib3.connectionpool": None,
    }):
        hook.install()  # must not raise
    hook.uninstall()  # idempotent


# ── Critical: double-record guard between requests + urllib3 ─────────────────


def test_layered_requests_urllib3_records_exactly_once(tmp_path: Path) -> None:
    """The defining test for the layering guard. When a `requests` call is
    made and internally goes through `urllib3`, exactly one model-call
    record must appear — not two. Both hooks see the call, but only the
    higher layer (RequestsHook) records it; Urllib3Hook checks the
    layering guard and skips.

    Without the guard in `_layering.py`, this test would fail with
    `len(records) == 2`.
    """
    from novafabric.capture.hooks._requests import RequestsHook
    from novafabric.capture.hooks._urllib3 import Urllib3Hook

    writer = make_writer(tmp_path)
    requests_hook = RequestsHook(writer=writer, parent_span_id="0" * 16)
    urllib3_hook = Urllib3Hook(writer=writer, parent_span_id="0" * 16)

    # Simulate `requests.Session.send` whose internal call to urllib3
    # we trigger explicitly inside the original. This mirrors what really
    # happens at runtime: requests calls Session.send → HTTPAdapter.send
    # → urllib3 PoolManager → HTTPConnectionPool.urlopen.
    pool = _fake_pool(host="api.openai.com")
    fake_resp = MagicMock(status_code=200)

    def fake_session_send(prepared_request, **kwargs):  # type: ignore[no-untyped-def]
        # Inside the request layer's "original send", urllib3 fires.
        # The Urllib3Hook should observe `is_inside_higher_layer() is True`
        # and skip recording.
        urllib3_hook._wrapped_urlopen(
            pool, "POST", "/v1/chat/completions",
            original=lambda m, u, **k: MagicMock(status=200),
            body=b'{"model":"gpt-4o","messages":[]}',
        )
        return fake_resp

    req = MagicMock()
    req.url = "https://api.openai.com/v1/chat/completions"
    req.body = b'{"model":"gpt-4o","messages":[]}'

    requests_hook._wrapped_send(req, original=fake_session_send)

    records = _model_calls(tmp_path)
    assert len(records) == 1, (
        f"expected exactly 1 record (only RequestsHook), got {len(records)}: "
        f"{[r['endpoint'] for r in records]}"
    )
    # And it must come from the higher layer — the higher hook owns
    # richer per-call context (e.g. PreparedRequest object).
    assert records[0]["gen_ai.system"] == "openai"


def test_urllib3_alone_records_when_no_higher_layer_active(tmp_path: Path) -> None:
    """Sanity: if no higher-layer hook is around the call, urllib3 records
    normally. Confirms the guard is opt-in, not always-on."""
    from novafabric.capture.hooks._urllib3 import Urllib3Hook

    writer = make_writer(tmp_path)
    hook = Urllib3Hook(writer=writer, parent_span_id="0" * 16)

    pool = _fake_pool(host="api.openai.com")
    hook._wrapped_urlopen(
        pool, "POST", "/v1/chat/completions",
        original=lambda m, u, **k: MagicMock(status=200),
        body=b'{"model":"gpt-4o","messages":[]}',
    )
    assert len(_model_calls(tmp_path)) == 1


def test_layering_guard_resets_after_higher_layer_returns(tmp_path: Path) -> None:
    """After the higher layer's call completes, the guard must reset so
    a *separate* unlayered urllib3 call (in a later span) records again.
    Verifies the contextmanager's reset-on-exit semantics."""
    from novafabric.capture.hooks._layering import (
        claim_higher_layer,
        is_inside_higher_layer,
    )
    from novafabric.capture.hooks._urllib3 import Urllib3Hook

    writer = make_writer(tmp_path)
    hook = Urllib3Hook(writer=writer, parent_span_id="0" * 16)
    pool = _fake_pool(host="api.openai.com")

    # First call: layered, urllib3 should skip.
    with claim_higher_layer():
        assert is_inside_higher_layer() is True
        hook._wrapped_urlopen(
            pool, "POST", "/v1/chat/completions",
            original=lambda m, u, **k: MagicMock(status=200),
            body=b'{"model":"gpt-4o","messages":[]}',
        )
    assert is_inside_higher_layer() is False
    assert _model_calls(tmp_path) == []

    # Second call: unlayered, urllib3 should record.
    hook._wrapped_urlopen(
        pool, "POST", "/v1/chat/completions",
        original=lambda m, u, **k: MagicMock(status=200),
        body=b'{"model":"gpt-4o","messages":[]}',
    )
    assert len(_model_calls(tmp_path)) == 1


def test_layering_guard_resets_on_exception(tmp_path: Path) -> None:
    """If the higher layer's wrapped call raises, the guard must still
    reset (try/finally semantics). Otherwise a single broken request
    would poison capture for the rest of the process."""
    from novafabric.capture.hooks._layering import (
        claim_higher_layer,
        is_inside_higher_layer,
    )

    try:
        with claim_higher_layer():
            assert is_inside_higher_layer() is True
            raise RuntimeError("simulated upstream failure")
    except RuntimeError:
        pass
    assert is_inside_higher_layer() is False


# ── hooks.__init__ defensive branches ────────────────────────────────────────


def test_is_sdk_available_returns_false_on_import_error(tmp_path: Path) -> None:
    import importlib.util

    from novafabric.capture.hooks import _is_sdk_available

    with patch.object(importlib.util, "find_spec", side_effect=ImportError("broken")):
        assert _is_sdk_available("some.sdk") is False


def test_is_sdk_available_returns_false_on_value_error(tmp_path: Path) -> None:
    import importlib.util

    from novafabric.capture.hooks import _is_sdk_available

    with patch.object(importlib.util, "find_spec", side_effect=ValueError("__spec__ is None")):
        assert _is_sdk_available("some.sdk") is False


def test_uninstall_all_swallows_hook_exception(tmp_path: Path) -> None:
    from novafabric.capture.hooks import _installed, uninstall_all

    bad_hook = MagicMock()
    bad_hook.uninstall.side_effect = RuntimeError("hook crashed")
    _installed.append(bad_hook)
    uninstall_all()  # must not raise
    assert _installed == []


# ── aiohttp _extract_body defensive branches ──────────────────────────────────


def test_aiohttp_extract_body_handles_bad_json_in_data(tmp_path: Path) -> None:
    from novafabric.capture.hooks._aiohttp import _extract_body

    result = _extract_body({"data": b"not-valid-json{{"})
    assert result == {}


def test_aiohttp_extract_body_handles_non_dict_json_in_data(tmp_path: Path) -> None:
    from novafabric.capture.hooks._aiohttp import _extract_body

    result = _extract_body({"data": b'["list", "not", "dict"]'})
    assert result == {}


def test_aiohttp_hook_raises_when_no_original_non_registry(tmp_path: Path) -> None:
    from novafabric.capture.hooks._aiohttp import AiohttpHook

    writer = make_writer(tmp_path)
    hook = AiohttpHook(writer=writer, parent_span_id="0" * 16)

    with pytest.raises(RuntimeError, match="original _request function required"):
        _run_async(hook._wrapped_request("GET", "https://example.com/api", original=None))


def test_aiohttp_hook_record_suppresses_writer_exception(tmp_path: Path) -> None:
    from novafabric.capture.hooks._aiohttp import AiohttpHook

    writer = make_writer(tmp_path)
    writer.append_model_call = MagicMock(side_effect=OSError("disk full"))  # type: ignore[method-assign]
    hook = AiohttpHook(writer=writer, parent_span_id="0" * 16)

    fake_resp = MagicMock(status=200)

    async def original(method, url, **kwargs):  # type: ignore[no-untyped-def]
        return fake_resp

    result = _run_async(
        hook._wrapped_request(
            "POST", "https://api.openai.com/v1/chat/completions",
            original=original, json={"model": "gpt-4o", "messages": []},
        )
    )
    assert result is fake_resp


# ── requests hook defensive branches ─────────────────────────────────────────


def test_requests_hook_handles_bad_json_body(tmp_path: Path) -> None:
    from novafabric.capture.hooks._requests import RequestsHook

    writer = make_writer(tmp_path)
    hook = RequestsHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url = "https://api.openai.com/v1/chat/completions"
    req.body = b"not-valid-json{{"
    fake_resp = MagicMock(status_code=200)

    hook._wrapped_send(req, original=MagicMock(return_value=fake_resp))
    records = _model_calls(tmp_path)
    assert len(records) == 1


def test_requests_hook_handles_non_dict_json_body(tmp_path: Path) -> None:
    from novafabric.capture.hooks._requests import RequestsHook

    writer = make_writer(tmp_path)
    hook = RequestsHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url = "https://api.openai.com/v1/chat/completions"
    req.body = b'["list"]'
    fake_resp = MagicMock(status_code=200)

    hook._wrapped_send(req, original=MagicMock(return_value=fake_resp))
    records = _model_calls(tmp_path)
    assert len(records) == 1


def test_requests_hook_record_suppresses_writer_exception(tmp_path: Path) -> None:
    from novafabric.capture.hooks._requests import RequestsHook

    writer = make_writer(tmp_path)
    writer.append_model_call = MagicMock(side_effect=OSError("disk full"))  # type: ignore[method-assign]
    hook = RequestsHook(writer=writer, parent_span_id="0" * 16)

    req = MagicMock()
    req.url = "https://api.openai.com/v1/chat/completions"
    req.body = b'{"model":"gpt-4o","messages":[]}'
    fake_resp = MagicMock(status_code=200)

    result = hook._wrapped_send(req, original=MagicMock(return_value=fake_resp))
    assert result is fake_resp


# ── urllib3 _extract_body_from_kwargs defensive branches ─────────────────────


def test_urllib3_extract_body_handles_bad_json_bytes(tmp_path: Path) -> None:
    from novafabric.capture.hooks._urllib3 import _extract_body_from_kwargs

    result = _extract_body_from_kwargs({"body": b"not-json{{"})
    assert result == {}


def test_urllib3_extract_body_handles_non_dict_bytes(tmp_path: Path) -> None:
    from novafabric.capture.hooks._urllib3 import _extract_body_from_kwargs

    result = _extract_body_from_kwargs({"body": b'["list"]'})
    assert result == {}


def test_urllib3_extract_body_handles_bad_json_str(tmp_path: Path) -> None:
    from novafabric.capture.hooks._urllib3 import _extract_body_from_kwargs

    result = _extract_body_from_kwargs({"body": "not-json{{"})
    assert result == {}


def test_urllib3_extract_body_handles_non_dict_str(tmp_path: Path) -> None:
    from novafabric.capture.hooks._urllib3 import _extract_body_from_kwargs

    result = _extract_body_from_kwargs({"body": '["list"]'})
    assert result == {}


# ── streaming-response NetworkEvent regression (2026-06-05) ──────────────────


def test_httpx_records_network_event_for_streaming_response(tmp_path: Path) -> None:
    """A streaming/unread httpx response raises on `.content` (ResponseNotRead).
    The NetworkEvent must STILL be recorded (size None), not silently dropped.
    Regression: ChatOllama streams chat, so this dropped every Ollama network
    event even though model calls were captured."""
    from novafabric.capture.event_recorder import set_current_recorder
    from novafabric.capture.hooks import install_all, uninstall_all
    from novafabric.capture.hooks._httpx import HttpxHook

    class _StreamingResp:
        status_code = 200

        @property
        def content(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("ResponseNotRead: body sent with stream=True")

    set_current_recorder(None)
    writer = make_writer(tmp_path)
    install_all(writer=writer, parent_span_id="0" * 16)
    try:
        hook = HttpxHook(writer=writer, parent_span_id="0" * 16)
        req = MagicMock()
        req.url.__str__ = MagicMock(return_value="http://localhost:11434/api/chat")
        req.content = b'{"model":"qwen3:30b-a3b","messages":[]}'
        req.method = "POST"
        hook._wrapped_send(req, original=MagicMock(return_value=_StreamingResp()))
    finally:
        uninstall_all()

    net_path = tmp_path / RUN_ID / "network_events.jsonl"
    assert net_path.exists(), "streaming response must still produce a NetworkEvent"
    rec = [json.loads(line) for line in net_path.read_text().splitlines() if line]
    assert len(rec) == 1
    assert rec[0]["host"] == "localhost"
    assert rec[0]["response_size_bytes"] is None  # size unavailable, but event kept


def test_safe_response_size_handles_unread_and_none() -> None:
    from novafabric.capture.hooks._otel_genai import safe_response_size

    class _Unread:
        @property
        def content(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("ResponseNotRead")

    assert safe_response_size(None) is None
    assert safe_response_size(_Unread()) is None
    assert safe_response_size(MagicMock(content=b"abc")) == 3


# ── ADR-0209 D2.4: aiohttp / urllib3 NetworkEvent parity ─────────────────────


def _network_events(tmp_path: Path) -> list[dict]:  # type: ignore[type-arg]
    path = tmp_path / RUN_ID / "network_events.jsonl"
    assert path.exists(), "network_events.jsonl must be written"
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_aiohttp_hook_writes_network_event_via_recorder(tmp_path: Path) -> None:
    """Parity with test_httpx_hook_writes_network_event_via_recorder: once a
    recorder is active, an aiohttp call produces a NetworkEvent record."""
    from novafabric.capture.event_recorder import EventRecorder, set_current_recorder
    from novafabric.capture.hooks._aiohttp import AiohttpHook

    writer = make_writer(tmp_path)
    set_current_recorder(EventRecorder(
        capsule_dir=writer.capsule_dir, run_id=RUN_ID, capsule_id=RUN_ID,
    ))
    hook = AiohttpHook(writer=writer, parent_span_id="0" * 16)

    fake_resp = MagicMock(status=200)

    async def original(method, url, **kwargs):  # type: ignore[no-untyped-def]
        return fake_resp

    _run_async(hook._wrapped_request(
        "POST", "https://api.openai.com/v1/chat/completions",
        original=original, json={"model": "gpt-4o", "messages": []},
    ))

    (event,) = _network_events(tmp_path)
    assert event["library"] == "aiohttp"
    assert event["method"] == "POST"
    assert event["host"] == "api.openai.com"
    assert event["url"] == "https://api.openai.com/v1/chat/completions"
    assert event["status_code"] == 200
    assert event["is_ai_api"] is True


def test_aiohttp_hook_no_network_event_without_recorder(tmp_path: Path) -> None:
    """No recorder installed (pre-ADR-0209 behavior) — no stream, no error."""
    from novafabric.capture.hooks._aiohttp import AiohttpHook

    writer = make_writer(tmp_path)
    hook = AiohttpHook(writer=writer, parent_span_id="0" * 16)

    async def original(method, url, **kwargs):  # type: ignore[no-untyped-def]
        return MagicMock(status=200)

    _run_async(hook._wrapped_request(
        "POST", "https://api.openai.com/v1/chat/completions",
        original=original, json={"model": "gpt-4o", "messages": []},
    ))
    assert not (tmp_path / RUN_ID / "network_events.jsonl").exists()


def test_aiohttp_hook_network_event_on_error(tmp_path: Path) -> None:
    from novafabric.capture.event_recorder import EventRecorder, set_current_recorder
    from novafabric.capture.hooks._aiohttp import AiohttpHook

    writer = make_writer(tmp_path)
    set_current_recorder(EventRecorder(
        capsule_dir=writer.capsule_dir, run_id=RUN_ID, capsule_id=RUN_ID,
    ))
    hook = AiohttpHook(writer=writer, parent_span_id="0" * 16)

    async def original(method, url, **kwargs):  # type: ignore[no-untyped-def]
        raise ConnectionError("boom")

    with pytest.raises(ConnectionError):
        _run_async(hook._wrapped_request(
            "POST", "https://api.anthropic.com/v1/messages",
            original=original, json={"model": "claude-sonnet-4-6", "messages": []},
        ))

    (event,) = _network_events(tmp_path)
    assert event["library"] == "aiohttp"
    assert event["status_code"] == 599


def test_urllib3_hook_writes_network_event_via_recorder(tmp_path: Path) -> None:
    from novafabric.capture.event_recorder import EventRecorder, set_current_recorder
    from novafabric.capture.hooks._urllib3 import Urllib3Hook

    writer = make_writer(tmp_path)
    set_current_recorder(EventRecorder(
        capsule_dir=writer.capsule_dir, run_id=RUN_ID, capsule_id=RUN_ID,
    ))
    hook = Urllib3Hook(writer=writer, parent_span_id="0" * 16)

    pool = _fake_pool(host="api.openai.com")
    hook._wrapped_urlopen(
        pool, "POST", "/v1/chat/completions",
        original=lambda m, u, **k: MagicMock(status=200),
        body=b'{"model":"gpt-4o","messages":[]}',
    )

    (event,) = _network_events(tmp_path)
    assert event["library"] == "urllib3"
    assert event["method"] == "POST"
    assert event["host"] == "api.openai.com"
    assert event["url"] == "https://api.openai.com/v1/chat/completions"
    assert event["is_ai_api"] is True


def test_layered_requests_urllib3_single_network_event(tmp_path: Path) -> None:
    """ADR-0209 layering regression: a requests-initiated urllib3 call
    produces exactly ONE NetworkEvent — from the requests layer."""
    from novafabric.capture.event_recorder import EventRecorder, set_current_recorder
    from novafabric.capture.hooks._requests import RequestsHook
    from novafabric.capture.hooks._urllib3 import Urllib3Hook

    writer = make_writer(tmp_path)
    set_current_recorder(EventRecorder(
        capsule_dir=writer.capsule_dir, run_id=RUN_ID, capsule_id=RUN_ID,
    ))
    requests_hook = RequestsHook(writer=writer, parent_span_id="0" * 16)
    urllib3_hook = Urllib3Hook(writer=writer, parent_span_id="0" * 16)

    pool = _fake_pool(host="api.openai.com")

    def fake_session_send(prepared_request, **kwargs):  # type: ignore[no-untyped-def]
        urllib3_hook._wrapped_urlopen(
            pool, "POST", "/v1/chat/completions",
            original=lambda m, u, **k: MagicMock(status=200),
            body=b'{"model":"gpt-4o","messages":[]}',
        )
        return MagicMock(status_code=200, content=b"{}")

    req = MagicMock()
    req.url = "https://api.openai.com/v1/chat/completions"
    req.body = b'{"model":"gpt-4o","messages":[]}'
    req.method = "POST"

    requests_hook._wrapped_send(req, original=fake_session_send)

    events = _network_events(tmp_path)
    assert len(events) == 1, (
        f"expected exactly 1 NetworkEvent, got {len(events)}: "
        f"{[e['library'] for e in events]}"
    )
    assert events[0]["library"] == "requests"
