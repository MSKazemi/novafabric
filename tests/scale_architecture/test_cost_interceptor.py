"""Tests for CostInterceptor (cap-002, ADR-0066)."""

from __future__ import annotations

import pytest

from novafabric.cost.interceptor import CostInterceptor
from novafabric.schemas.event_schema import CostFacet

# ---------------------------------------------------------------------------
# Helpers — fake SDK response objects
# ---------------------------------------------------------------------------


class _FakeUsageOpenAI:
    def __init__(self, prompt: int, completion: int, cached: int = 0) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion

        class _Details:
            def __init__(self, c: int) -> None:
                self.cached_tokens = c

        self.prompt_tokens_details = _Details(cached)


class _FakeResponseOpenAI:
    def __init__(self, prompt: int, completion: int, cached: int = 0) -> None:
        self.usage = _FakeUsageOpenAI(prompt, completion, cached)


class _FakeUsageAnthropic:
    def __init__(self, inp: int, out: int, cached: int = 0) -> None:
        self.input_tokens = inp
        self.output_tokens = out
        self.cache_read_input_tokens = cached


class _FakeResponseAnthropic:
    def __init__(self, inp: int, out: int, cached: int = 0) -> None:
        self.usage = _FakeUsageAnthropic(inp, out, cached)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExtractFromOpenAIResponse:
    def test_dict_response(self) -> None:
        resp = {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "prompt_tokens_details": {"cached_tokens": 10},
            }
        }
        facet = CostInterceptor.extract_from_openai_response(resp, "gpt-4o")
        assert isinstance(facet, CostFacet)
        assert facet.input_tokens == 100
        assert facet.completion_tokens == 50
        assert facet.cached_tokens == 10
        assert facet.provider == "openai"
        assert facet.model_id == "gpt-4o"
        assert facet.cost_usd_estimated > 0

    def test_object_response(self) -> None:
        resp = _FakeResponseOpenAI(prompt=200, completion=80, cached=20)
        facet = CostInterceptor.extract_from_openai_response(resp, "gpt-4o-mini")
        assert facet.input_tokens == 200
        assert facet.completion_tokens == 80
        assert facet.cached_tokens == 20

    def test_all_mandatory_fields_populated(self) -> None:
        resp = {"usage": {"prompt_tokens": 50, "completion_tokens": 25}}
        facet = CostInterceptor.extract_from_openai_response(resp, "gpt-4o")
        assert facet.model_id is not None
        assert facet.provider is not None
        assert isinstance(facet.input_tokens, int)
        assert isinstance(facet.completion_tokens, int)
        assert isinstance(facet.cost_usd_estimated, float)


class TestExtractFromAnthropicResponse:
    def test_dict_response(self) -> None:
        resp = {
            "usage": {
                "input_tokens": 120,
                "output_tokens": 60,
                "cache_read_input_tokens": 5,
            }
        }
        facet = CostInterceptor.extract_from_anthropic_response(
            resp, "claude-3-5-sonnet"
        )
        assert facet.input_tokens == 120
        assert facet.completion_tokens == 60
        assert facet.cached_tokens == 5
        assert facet.provider == "anthropic"

    def test_object_response(self) -> None:
        resp = _FakeResponseAnthropic(inp=300, out=150, cached=30)
        facet = CostInterceptor.extract_from_anthropic_response(resp, "claude-3-opus")
        assert facet.input_tokens == 300
        assert facet.completion_tokens == 150
        assert facet.cached_tokens == 30

    def test_all_mandatory_fields_populated(self) -> None:
        resp = {"usage": {"input_tokens": 10, "output_tokens": 5}}
        facet = CostInterceptor.extract_from_anthropic_response(resp, "claude-3-haiku")
        assert facet.model_id is not None
        assert facet.provider == "anthropic"
        assert isinstance(facet.cost_usd_estimated, float)


class TestUnknownModel:
    def test_openai_unknown_model_returns_zero_cost(self) -> None:
        resp = {"usage": {"prompt_tokens": 100, "completion_tokens": 50}}
        facet = CostInterceptor.extract_from_openai_response(resp, "gpt-99-ultra")
        assert facet.cost_usd_estimated == pytest.approx(0.0)

    def test_anthropic_unknown_model_returns_zero_cost(self) -> None:
        resp = {"usage": {"input_tokens": 100, "output_tokens": 50}}
        facet = CostInterceptor.extract_from_anthropic_response(resp, "claude-99-mega")
        assert facet.cost_usd_estimated == pytest.approx(0.0)
