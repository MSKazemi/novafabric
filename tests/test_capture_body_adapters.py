"""Contract tests for the body adapter system (C-3.3b).

Adapters take a non-OpenAI-shaped request body (e.g. AWS Bedrock's
per-provider shapes) and return an OpenAI-shaped dict that the
shared OTel GenAI extractor can consume. This file is the contract:
if these tests pass, the wire-level hooks see consistent records
across providers.
"""
from __future__ import annotations

from typing import Any

import pytest

from novafabric.capture.body_adapters import (
    AdapterProtocol,
    UnknownAdapterError,
    adapt_body,
    get_adapter,
    known_adapter_names,
)

# ── registry contract ────────────────────────────────────────────────────────


def test_known_names_includes_built_in_bedrock_adapters() -> None:
    names = set(known_adapter_names())
    assert "aws.bedrock.anthropic" in names
    assert "aws.bedrock.cohere" in names
    assert "aws.bedrock.titan" in names
    assert "aws.bedrock.llama" in names


def test_known_names_is_sorted() -> None:
    names = known_adapter_names()
    assert names == sorted(names)


def test_get_adapter_returns_callable_satisfying_protocol() -> None:
    adapter: AdapterProtocol = get_adapter("aws.bedrock.anthropic")
    # Bare structural check — adapters expose .name and are callable.
    assert adapter.name == "aws.bedrock.anthropic"
    assert callable(adapter.adapt)


def test_unknown_adapter_raises_with_helpful_message() -> None:
    with pytest.raises(UnknownAdapterError) as excinfo:
        get_adapter("fictional.provider")
    msg = str(excinfo.value)
    assert "fictional.provider" in msg
    assert "Available" in msg
    assert "aws.bedrock.anthropic" in msg


# ── adapt_body dispatcher: URL → adapter → normalized body ───────────────────


def test_adapt_body_passthrough_when_no_url_match() -> None:
    """OpenAI-shaped URLs are not in the adapter registry; the body
    must be returned unchanged so the hooks fall through to the
    generic extractor."""
    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    result = adapt_body(body, url="https://api.openai.com/v1/chat/completions")
    assert result == body


def test_adapt_body_passthrough_for_empty_url() -> None:
    body = {"model": "x"}
    assert adapt_body(body, url="") == body


def test_adapt_body_returns_dict_for_invalid_input() -> None:
    """Adapter must never raise on weird input — capture is a recorder,
    not a validator."""
    result = adapt_body("not a dict", url="https://bedrock-runtime.us-east-1.amazonaws.com/...")  # type: ignore[arg-type]
    assert isinstance(result, dict)


# ── Bedrock URL → adapter resolution ─────────────────────────────────────────


@pytest.mark.parametrize("model_id, expected_adapter", [
    ("anthropic.claude-haiku-4-5-v1:0", "aws.bedrock.anthropic"),
    ("anthropic.claude-sonnet-4-7-v1:0", "aws.bedrock.anthropic"),
    ("cohere.command-r-plus-v1:0", "aws.bedrock.cohere"),
    ("cohere.embed-english-v3", "aws.bedrock.cohere"),
    ("amazon.titan-text-express-v1", "aws.bedrock.titan"),
    ("amazon.nova-pro-v1:0", "aws.bedrock.titan"),  # Nova series uses Titan-style
    ("meta.llama3-70b-instruct-v1:0", "aws.bedrock.llama"),
    ("meta.llama4-maverick-17b-instruct-v1:0", "aws.bedrock.llama"),
])
def test_bedrock_model_id_routes_to_correct_adapter(
    model_id: str, expected_adapter: str
) -> None:
    """The Bedrock URL has the model id in the path:
    /model/<modelId>/invoke. The dispatcher uses the URL prefix to
    pick the adapter."""
    from novafabric.capture.body_adapters import _resolve_adapter_name_for_url

    url = f"https://bedrock-runtime.us-east-1.amazonaws.com/model/{model_id}/invoke"
    assert _resolve_adapter_name_for_url(url) == expected_adapter


def test_bedrock_url_for_unknown_provider_returns_none() -> None:
    from novafabric.capture.body_adapters import _resolve_adapter_name_for_url

    url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/unknown.brand-foo-v1/invoke"
    assert _resolve_adapter_name_for_url(url) is None


# ── per-provider adapter behavior ────────────────────────────────────────────


class TestBedrockAnthropicAdapter:
    """Bedrock-Anthropic body shape:
    {anthropic_version, max_tokens, messages, system?, temperature?, ...}
    The model id is in the URL, NOT the body."""

    def test_extracts_messages_unchanged(self) -> None:
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 256,
            "messages": [{"role": "user", "content": "hi"}],
        }
        url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/anthropic.claude-haiku-4-5-v1:0/invoke"
        result = adapt_body(body, url=url)
        assert result["messages"] == body["messages"]

    def test_injects_model_id_from_url_into_body(self) -> None:
        body = {"anthropic_version": "x", "max_tokens": 256, "messages": []}
        url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/anthropic.claude-haiku-4-5-v1:0/invoke"
        result = adapt_body(body, url=url)
        assert result["model"] == "anthropic.claude-haiku-4-5-v1:0"

    def test_url_decodes_colon_in_model_id(self) -> None:
        """Bedrock URLs URL-encode the colon in model ids:
        anthropic.claude-haiku-4-5-v1%3A0"""
        body = {"anthropic_version": "x", "max_tokens": 256, "messages": []}
        url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/anthropic.claude-haiku-4-5-v1%3A0/invoke"
        result = adapt_body(body, url=url)
        assert result["model"] == "anthropic.claude-haiku-4-5-v1:0"

    def test_preserves_request_side_semconv_fields(self) -> None:
        body = {
            "anthropic_version": "x", "max_tokens": 1024,
            "messages": [], "temperature": 0.7,
            "top_p": 0.95, "top_k": 40,
            "stop_sequences": ["END"],
        }
        url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/anthropic.claude-haiku-4-5-v1:0/invoke"
        result = adapt_body(body, url=url)
        # All semconv-bearing fields must survive.
        assert result["max_tokens"] == 1024
        assert result["temperature"] == 0.7
        assert result["top_p"] == 0.95
        assert result["top_k"] == 40
        assert result["stop_sequences"] == ["END"]


class TestBedrockCohereAdapter:
    """Bedrock-Cohere body shape:
    {prompt, max_tokens, temperature?, p?, k?, stop_sequences?, ...}
    Single string prompt — no messages array."""

    def test_synthesizes_messages_from_prompt(self) -> None:
        body = {"prompt": "Summarize this.", "max_tokens": 256}
        url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/cohere.command-r-plus-v1:0/invoke"
        result = adapt_body(body, url=url)
        assert result["messages"] == [
            {"role": "user", "content": "Summarize this."}
        ]

    def test_injects_model_id(self) -> None:
        body = {"prompt": "x", "max_tokens": 100}
        url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/cohere.command-r-plus-v1:0/invoke"
        result = adapt_body(body, url=url)
        assert result["model"] == "cohere.command-r-plus-v1:0"

    def test_normalizes_p_to_top_p(self) -> None:
        """Cohere uses 'p' for top_p and 'k' for top_k. Normalize."""
        body = {"prompt": "x", "max_tokens": 100, "p": 0.9, "k": 5}
        url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/cohere.command-r-plus-v1:0/invoke"
        result = adapt_body(body, url=url)
        assert result.get("top_p") == 0.9
        assert result.get("top_k") == 5

    def test_preserves_temperature_and_stop(self) -> None:
        body = {
            "prompt": "x", "max_tokens": 100,
            "temperature": 0.5, "stop_sequences": ["END"],
        }
        url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/cohere.command-r-plus-v1:0/invoke"
        result = adapt_body(body, url=url)
        assert result["temperature"] == 0.5
        assert result["stop_sequences"] == ["END"]


class TestBedrockTitanAdapter:
    """Bedrock-Titan body shape:
    {inputText, textGenerationConfig: {maxTokenCount, temperature, topP, stopSequences}}
    Nested config — flatten."""

    def test_synthesizes_messages_from_inputText(self) -> None:
        body = {
            "inputText": "Tell me a joke.",
            "textGenerationConfig": {"maxTokenCount": 100},
        }
        url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/amazon.titan-text-express-v1/invoke"
        result = adapt_body(body, url=url)
        assert result["messages"] == [
            {"role": "user", "content": "Tell me a joke."}
        ]

    def test_flattens_textGenerationConfig(self) -> None:
        body = {
            "inputText": "x",
            "textGenerationConfig": {
                "maxTokenCount": 512,
                "temperature": 0.6,
                "topP": 0.9,
                "stopSequences": ["END"],
            },
        }
        url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/amazon.titan-text-express-v1/invoke"
        result = adapt_body(body, url=url)
        assert result["max_tokens"] == 512
        assert result["temperature"] == 0.6
        assert result["top_p"] == 0.9
        assert result["stop_sequences"] == ["END"]

    def test_handles_missing_textGenerationConfig(self) -> None:
        """Titan body is allowed to omit textGenerationConfig (uses model
        defaults). Adapter must not crash."""
        body = {"inputText": "x"}
        url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/amazon.titan-text-express-v1/invoke"
        result = adapt_body(body, url=url)
        assert result["model"] == "amazon.titan-text-express-v1"


class TestBedrockLlamaAdapter:
    """Bedrock-Llama body shape:
    {prompt, max_gen_len, temperature?, top_p?}
    Llama-style — single prompt, max_gen_len instead of max_tokens."""

    def test_synthesizes_messages_from_prompt(self) -> None:
        body = {"prompt": "Hello", "max_gen_len": 100}
        url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/meta.llama3-70b-instruct-v1:0/invoke"
        result = adapt_body(body, url=url)
        assert result["messages"] == [{"role": "user", "content": "Hello"}]

    def test_normalizes_max_gen_len_to_max_tokens(self) -> None:
        body = {"prompt": "x", "max_gen_len": 256}
        url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/meta.llama3-70b-instruct-v1:0/invoke"
        result = adapt_body(body, url=url)
        assert result["max_tokens"] == 256

    def test_preserves_temperature_and_top_p(self) -> None:
        body = {
            "prompt": "x", "max_gen_len": 100,
            "temperature": 0.4, "top_p": 0.9,
        }
        url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/meta.llama3-70b-instruct-v1:0/invoke"
        result = adapt_body(body, url=url)
        assert result["temperature"] == 0.4
        assert result["top_p"] == 0.9


# ── End-to-end: adapter + extract_request_attributes ─────────────────────────


def test_bedrock_anthropic_full_pipeline_yields_correct_semconv() -> None:
    """The promise of C-3.3b: a Bedrock-Anthropic call ends up with
    gen_ai.request.model populated correctly (was 'unknown' before
    adapters). Hooks pass the RAW body to extract_request_attributes,
    which calls adapt_body internally (post C-3.3b wiring)."""
    from novafabric.capture.hooks._otel_genai import extract_request_attributes

    raw_bedrock_body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.7,
    }
    url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/anthropic.claude-haiku-4-5-v1:0/invoke"
    attrs = extract_request_attributes(
        raw_bedrock_body, url=url, gen_ai_system="aws.bedrock",
    )
    # Before C-3.3b: gen_ai.request.model would be 'unknown'.
    assert attrs["gen_ai.request.model"] == "anthropic.claude-haiku-4-5-v1:0"
    assert attrs["gen_ai.request.max_tokens"] == 1024
    assert attrs["gen_ai.request.temperature"] == 0.7
    assert attrs["gen_ai.request.messages"] == raw_bedrock_body["messages"]


def test_bedrock_cohere_full_pipeline_yields_correct_semconv() -> None:
    from novafabric.capture.hooks._otel_genai import extract_request_attributes

    raw = {"prompt": "Summarize.", "max_tokens": 200, "temperature": 0.3}
    url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/cohere.command-r-plus-v1:0/invoke"
    attrs = extract_request_attributes(
        raw, url=url, gen_ai_system="aws.bedrock",
    )
    assert attrs["gen_ai.request.model"] == "cohere.command-r-plus-v1:0"
    assert attrs["gen_ai.request.max_tokens"] == 200
    assert attrs["gen_ai.request.messages"] == [
        {"role": "user", "content": "Summarize."}
    ]


def test_adapt_body_is_idempotent_for_openai_shaped_input() -> None:
    """Defense in depth: calling adapt_body twice on the same Bedrock body
    must not lose information. The first call normalizes; the second is
    a no-op because the body is already OpenAI-shaped."""
    raw = {"prompt": "hi", "max_tokens": 100}
    url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/cohere.command-r-plus-v1:0/invoke"
    once = adapt_body(raw, url=url)
    twice = adapt_body(once, url=url)
    assert once == twice


# ── Plugin contract: third-party adapters via entry points ───────────────────


def test_register_third_party_adapter() -> None:
    """The adapter registry must accept a runtime-registered adapter so
    a plugin's importable can extend coverage without a core change."""
    from novafabric.capture.body_adapters import (
        register_adapter,
        unregister_adapter,
    )

    class _FakeAdapter:
        name = "test.fake.provider"

        def adapt(self, body: dict[str, Any], url: str) -> dict[str, Any]:
            return {"model": "test-fake", "messages": [{"role": "user", "content": "x"}]}

    fake: AdapterProtocol = _FakeAdapter()
    try:
        register_adapter(fake)
        assert "test.fake.provider" in known_adapter_names()
        assert get_adapter("test.fake.provider") is fake
    finally:
        unregister_adapter("test.fake.provider")
    assert "test.fake.provider" not in known_adapter_names()


def test_register_adapter_raises_on_duplicate_name() -> None:
    from novafabric.capture.body_adapters import register_adapter, unregister_adapter

    class _DupAdapter:
        name = "test.dup.provider"

        def adapt(self, body: dict[str, Any], url: str) -> dict[str, Any]:
            return body

    dup: AdapterProtocol = _DupAdapter()
    try:
        register_adapter(dup)
        with pytest.raises(ValueError, match="already registered"):
            register_adapter(dup)
    finally:
        unregister_adapter("test.dup.provider")


def test_adapt_body_falls_back_on_unknown_adapter_name(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """If the URL resolves to an adapter name not in the registry,
    adapt_body must return the original body unchanged."""
    # Patch the resolver to return a name that doesn't exist in registry.
    monkeypatch.setattr(
        "novafabric.capture.body_adapters._resolve_adapter_name_for_url",
        lambda url: "nonexistent.adapter",
    )
    body = {"model": "test", "messages": []}
    result = adapt_body(body, url="https://bedrock-runtime.us-east-1.amazonaws.com/x")
    assert result == body


def test_adapt_body_falls_back_when_adapter_raises(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A broken adapter must not surface to the caller."""
    from novafabric.capture.body_adapters import register_adapter, unregister_adapter

    class _BrokenAdapter:
        name = "test.broken.provider"

        def adapt(self, body: dict[str, Any], url: str) -> dict[str, Any]:
            raise RuntimeError("adapter is broken")

    broken: AdapterProtocol = _BrokenAdapter()

    monkeypatch.setattr(
        "novafabric.capture.body_adapters._resolve_adapter_name_for_url",
        lambda url: "test.broken.provider",
    )
    try:
        register_adapter(broken)
        body = {"model": "test", "messages": []}
        result = adapt_body(body, url="https://bedrock-runtime.us-east-1.amazonaws.com/x")
        assert result == body
    finally:
        unregister_adapter("test.broken.provider")
