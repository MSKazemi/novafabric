"""Unit tests for the shared OTel GenAI attribute extractor (C-4 phase 1).

The extractor is the single source of truth wire-level hooks call into.
If a field stops being extracted here, every wire-level hook silently
loses it; these tests are the contract.
"""
from __future__ import annotations

import pytest

from novafabric.capture.hooks._otel_genai import (
    build_record_envelope,
    extract_request_attributes,
)


class TestExtractRequestAttributes:
    def test_minimum_body_yields_required_envelope(self) -> None:
        attrs = extract_request_attributes(
            {"model": "gpt-4o", "messages": []},
            url="https://api.openai.com/v1/chat/completions",
            gen_ai_system="openai",
        )
        assert attrs["gen_ai.system"] == "openai"
        assert attrs["gen_ai.operation.name"] == "chat"
        assert attrs["gen_ai.request.model"] == "gpt-4o"
        assert attrs["gen_ai.response.model"] == "gpt-4o"
        assert attrs["gen_ai.request.messages"] == []
        assert attrs["gen_ai.response.choices"] == []
        assert attrs["gen_ai.usage.input_tokens"] == 0
        assert attrs["gen_ai.usage.output_tokens"] == 0
        assert attrs["endpoint"] == "https://api.openai.com/v1/chat/completions"

    def test_temperature_extracted_when_present(self) -> None:
        attrs = extract_request_attributes({"model": "x", "temperature": 0.7})
        assert attrs["gen_ai.request.temperature"] == 0.7

    def test_temperature_omitted_when_absent(self) -> None:
        attrs = extract_request_attributes({"model": "x"})
        assert "gen_ai.request.temperature" not in attrs

    def test_max_tokens_extracted(self) -> None:
        attrs = extract_request_attributes({"model": "x", "max_tokens": 1024})
        assert attrs["gen_ai.request.max_tokens"] == 1024

    def test_top_p_extracted(self) -> None:
        attrs = extract_request_attributes({"model": "x", "top_p": 0.9})
        assert attrs["gen_ai.request.top_p"] == 0.9

    def test_top_k_extracted(self) -> None:
        attrs = extract_request_attributes({"model": "x", "top_k": 40})
        assert attrs["gen_ai.request.top_k"] == 40

    def test_frequency_penalty_extracted(self) -> None:
        attrs = extract_request_attributes({"model": "x", "frequency_penalty": 0.5})
        assert attrs["gen_ai.request.frequency_penalty"] == 0.5

    def test_presence_penalty_extracted(self) -> None:
        attrs = extract_request_attributes({"model": "x", "presence_penalty": 0.3})
        assert attrs["gen_ai.request.presence_penalty"] == 0.3

    def test_seed_extracted(self) -> None:
        attrs = extract_request_attributes({"model": "x", "seed": 42})
        assert attrs["gen_ai.request.seed"] == 42

    def test_seed_critical_for_exact_replay(self) -> None:
        """Per design/spec/model-call-v0.md, seed is 'critical for exact replay'.
        Regression test: if extraction breaks, exact-replay determinism is lost."""
        attrs = extract_request_attributes({
            "model": "llama3.1:70b", "temperature": 0.0, "seed": 12345,
        })
        assert attrs["gen_ai.request.seed"] == 12345
        assert attrs["gen_ai.request.temperature"] == 0.0


class TestStopSequenceNormalization:
    def test_anthropic_stop_sequences_list(self) -> None:
        attrs = extract_request_attributes({
            "model": "claude-sonnet-4-6", "stop_sequences": ["END", "\n\n"],
        })
        assert attrs["gen_ai.request.stop_sequences"] == ["END", "\n\n"]

    def test_openai_stop_string(self) -> None:
        attrs = extract_request_attributes({"model": "gpt-4o", "stop": "END"})
        assert attrs["gen_ai.request.stop_sequences"] == ["END"]

    def test_openai_stop_list(self) -> None:
        attrs = extract_request_attributes({"model": "gpt-4o", "stop": ["END", "\n"]})
        assert attrs["gen_ai.request.stop_sequences"] == ["END", "\n"]

    def test_no_stop_omits_field(self) -> None:
        attrs = extract_request_attributes({"model": "x"})
        assert "gen_ai.request.stop_sequences" not in attrs

    def test_invalid_stop_type_omits_field(self) -> None:
        # Some weird body where `stop` is a number — skip silently, never crash.
        attrs = extract_request_attributes({"model": "x", "stop": 42})
        assert "gen_ai.request.stop_sequences" not in attrs

    def test_stop_sequences_takes_precedence_over_stop(self) -> None:
        attrs = extract_request_attributes({
            "model": "x",
            "stop_sequences": ["A"],
            "stop": "B",
        })
        # When both present, prefer the canonical Anthropic field.
        assert attrs["gen_ai.request.stop_sequences"] == ["A"]


class TestChoiceCount:
    def test_n_extracted_when_positive_int(self) -> None:
        attrs = extract_request_attributes({"model": "x", "n": 3})
        assert attrs["gen_ai.request.choice.count"] == 3

    def test_n_zero_omitted(self) -> None:
        # 0 is invalid per spec semantics; treat as absent.
        attrs = extract_request_attributes({"model": "x", "n": 0})
        assert "gen_ai.request.choice.count" not in attrs


class TestTypeSafety:
    def test_temperature_string_omitted(self) -> None:
        # Body might have a stringified value (rare but possible). Skip
        # rather than emit a malformed record.
        attrs = extract_request_attributes({"model": "x", "temperature": "0.7"})
        assert "gen_ai.request.temperature" not in attrs

    def test_max_tokens_float_omitted(self) -> None:
        # max_tokens is integer-only per OTel spec; reject floats.
        attrs = extract_request_attributes({"model": "x", "max_tokens": 1024.5})
        assert "gen_ai.request.max_tokens" not in attrs

    def test_non_dict_body_treated_as_empty(self) -> None:
        attrs = extract_request_attributes("not a dict", url="http://x", gen_ai_system="x")  # type: ignore[arg-type]
        assert attrs["gen_ai.request.model"] == "unknown"
        assert attrs["gen_ai.request.messages"] == []

    def test_messages_default_empty_list_when_absent(self) -> None:
        attrs = extract_request_attributes({"model": "x"})
        assert attrs["gen_ai.request.messages"] == []


class TestProviderShapesEndToEnd:
    """Sanity-check the extractor against realistic OpenAI and Anthropic
    request bodies — the two primary shapes the wire-level hooks see."""

    def test_realistic_openai_chat_completion_body(self) -> None:
        body = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": "You are concise."},
                {"role": "user", "content": "What is 2+2?"},
            ],
            "temperature": 0.0,
            "max_tokens": 100,
            "top_p": 1.0,
            "frequency_penalty": 0.0,
            "presence_penalty": 0.0,
            "seed": 42,
            "n": 1,
            "stop": ["\n\n"],
        }
        attrs = extract_request_attributes(
            body, url="https://api.openai.com/v1/chat/completions",
            gen_ai_system="openai",
        )
        assert attrs["gen_ai.request.model"] == "gpt-4o-mini"
        assert len(attrs["gen_ai.request.messages"]) == 2
        assert attrs["gen_ai.request.temperature"] == 0.0
        assert attrs["gen_ai.request.max_tokens"] == 100
        assert attrs["gen_ai.request.top_p"] == 1.0
        assert attrs["gen_ai.request.seed"] == 42
        assert attrs["gen_ai.request.choice.count"] == 1
        assert attrs["gen_ai.request.stop_sequences"] == ["\n\n"]

    def test_realistic_anthropic_messages_body(self) -> None:
        body = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "Summarize the file."}],
            "temperature": 0.7,
            "top_p": 0.95,
            "top_k": 40,
            "stop_sequences": ["END_OF_RESPONSE"],
        }
        attrs = extract_request_attributes(
            body, url="https://api.anthropic.com/v1/messages",
            gen_ai_system="anthropic",
        )
        assert attrs["gen_ai.request.model"] == "claude-sonnet-4-6"
        assert attrs["gen_ai.request.max_tokens"] == 1024
        assert attrs["gen_ai.request.temperature"] == 0.7
        assert attrs["gen_ai.request.top_p"] == 0.95
        assert attrs["gen_ai.request.top_k"] == 40
        assert attrs["gen_ai.request.stop_sequences"] == ["END_OF_RESPONSE"]


class TestRecordEnvelope:
    def test_envelope_has_required_metadata_fields(self) -> None:
        env = build_record_envelope(
            model_call_id="01ULID",
            parent_span_id="0123456789abcdef",
            started_at="2026-05-09T12:00:00.000000Z",
            finished_at="2026-05-09T12:00:01.000000Z",
            duration_ms=1000,
            status="success",
        )
        # These are the metadata fields every record needs (per
        # design/spec/model-call-v0.md "Required fields" table).
        for required in (
            "schema_version", "semconv_version", "model_call_id",
            "parent_span_id", "started_at", "finished_at",
            "duration_ms", "status",
        ):
            assert required in env

    @pytest.mark.parametrize("status", ["success", "error", "timeout", "denied", "partial"])
    def test_status_values_round_trip(self, status: str) -> None:
        env = build_record_envelope(
            model_call_id="x", parent_span_id="x",
            started_at="x", finished_at="x", duration_ms=0,
            status=status,
        )
        assert env["status"] == status
