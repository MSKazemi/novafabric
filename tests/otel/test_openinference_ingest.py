"""OpenInference → GenAI mapping on OTLP ingest (ADR-0098).

Before this, `genai_ingest` classified purely on `gen_ai.*`, so every span
emitted by the OpenInference ecosystem (LangChain, LlamaIndex, CrewAI, DSPy,
Phoenix) landed as `unclassified` and was dropped.

Covers: the translation table; that native `gen_ai.*` always wins over a
translated value; that content stays on the ADR-0021 content path; that
nothing is fabricated or silently dropped; and end-to-end ingest of
framework-shaped payloads.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from novafabric.otel.genai_ingest import ingest_otlp_json
from novafabric.otel.openinference import (
    SPAN_KIND_TO_OPERATION,
    is_openinference_span,
    translate_attributes,
)

# ---------------------------------------------------------------------------
# Translation table
# ---------------------------------------------------------------------------


def test_llm_span_becomes_a_model_call_vocabulary() -> None:
    out = translate_attributes(
        {
            "openinference.span.kind": "LLM",
            "llm.model_name": "gpt-4o",
            "llm.provider": "openai",
            "llm.token_count.prompt": 120,
            "llm.token_count.completion": 45,
        }
    )
    assert out["gen_ai.operation.name"] == "chat"
    assert out["gen_ai.request.model"] == "gpt-4o"
    assert out["gen_ai.system"] == "openai"
    assert out["gen_ai.usage.input_tokens"] == 120
    assert out["gen_ai.usage.output_tokens"] == 45
    # Originals are kept, so the unmapped/dropped accounting still sees them.
    assert out["llm.model_name"] == "gpt-4o"


def test_tool_span_maps_to_execute_tool() -> None:
    out = translate_attributes(
        {
            "openinference.span.kind": "TOOL",
            "tool.name": "search_web",
            "tool.description": "search the web",
            "tool_call.id": "call_7",
        }
    )
    assert out["gen_ai.operation.name"] == "execute_tool"
    assert out["gen_ai.tool.name"] == "search_web"
    assert out["gen_ai.tool.description"] == "search the web"
    assert out["gen_ai.tool.call.id"] == "call_7"


@pytest.mark.parametrize("kind,operation", sorted(SPAN_KIND_TO_OPERATION.items()))
def test_every_declared_span_kind_maps_to_a_classifiable_operation(
    kind: str, operation: str
) -> None:
    """A kind in the table must produce an operation `_classify` understands."""
    from novafabric.otel.genai_ingest import _AGENT_OPERATIONS, _MODEL_OPERATIONS

    assert operation in (_MODEL_OPERATIONS | _AGENT_OPERATIONS | {"execute_tool"}), (
        f"span kind {kind} maps to {operation!r}, which _classify does not route"
    )


def test_span_kind_is_case_insensitive() -> None:
    assert translate_attributes(
        {"openinference.span.kind": "llm", "llm.model_name": "m"}
    )["gen_ai.operation.name"] == "chat"


def test_unknown_span_kind_is_not_invented() -> None:
    """An unrecognised kind must not get a guessed operation."""
    out = translate_attributes(
        {"openinference.span.kind": "SOMETHING_NEW", "llm.model_name": "m"}
    )
    assert "gen_ai.operation.name" not in out
    assert out["gen_ai.request.model"] == "m"  # the parts we DO know still map


# ---------------------------------------------------------------------------
# Precedence and non-fabrication
# ---------------------------------------------------------------------------


def test_native_gen_ai_attributes_always_win() -> None:
    """Dual-emitting instrumentations: the native value is authoritative."""
    out = translate_attributes(
        {
            "openinference.span.kind": "LLM",
            "llm.model_name": "from-openinference",
            "gen_ai.request.model": "from-genai",
            "gen_ai.operation.name": "text_completion",
        }
    )
    assert out["gen_ai.request.model"] == "from-genai"
    assert out["gen_ai.operation.name"] == "text_completion"


def test_absent_attributes_are_never_fabricated() -> None:
    out = translate_attributes({"openinference.span.kind": "LLM"})
    assert "gen_ai.request.model" not in out
    assert "gen_ai.usage.input_tokens" not in out
    assert "gen_ai.system" not in out


def test_non_openinference_span_passes_through_untouched() -> None:
    attrs = {"gen_ai.request.model": "claude", "http.method": "POST"}
    assert translate_attributes(attrs) == attrs


def test_detection_is_strict_about_unrelated_spans() -> None:
    assert not is_openinference_span({"http.method": "GET"})
    assert not is_openinference_span({"llm.something.we.do.not.map": 1})
    assert is_openinference_span({"openinference.span.kind": "LLM"})
    assert is_openinference_span({"llm.model_name": "m"})


# ---------------------------------------------------------------------------
# Content — must stay on the ADR-0021 path
# ---------------------------------------------------------------------------


def test_content_maps_onto_the_gen_ai_content_keys(
) -> None:
    """input/output values must land on content keys, not a second route."""
    from novafabric.otel.genai_ingest import _CONTENT_KEYS

    out = translate_attributes(
        {
            "openinference.span.kind": "LLM",
            "input.value": "what is the capital of France?",
            "output.value": "Paris",
        }
    )
    assert out["gen_ai.input.messages"] == "what is the capital of France?"
    assert out["gen_ai.output.messages"] == "Paris"
    # Both targets are recognised content keys, so they follow the same policy.
    assert {"gen_ai.input.messages", "gen_ai.output.messages"} <= _CONTENT_KEYS


def test_no_content_attributes_means_no_content_keys() -> None:
    out = translate_attributes(
        {"openinference.span.kind": "LLM", "llm.model_name": "gpt-4o"}
    )
    assert not any(k.startswith("gen_ai.input") or k.startswith("gen_ai.output") for k in out)


# ---------------------------------------------------------------------------
# invocation parameters (a JSON string blob)
# ---------------------------------------------------------------------------


def test_invocation_parameters_are_lifted_to_first_class_attributes() -> None:
    out = translate_attributes(
        {
            "openinference.span.kind": "LLM",
            "llm.invocation_parameters": json.dumps(
                {"temperature": 0.7, "max_tokens": 512, "unknown_knob": 3}
            ),
        }
    )
    assert out["gen_ai.request.temperature"] == 0.7
    assert out["gen_ai.request.max_tokens"] == 512
    # The blob is left intact, so the unrecognised knob is not lost.
    assert "unknown_knob" in out["llm.invocation_parameters"]


@pytest.mark.parametrize("blob", ["not json", "", "   ", "[1,2,3]", "null"])
def test_malformed_invocation_parameters_never_raise(blob: str) -> None:
    out = translate_attributes(
        {"openinference.span.kind": "LLM", "llm.invocation_parameters": blob}
    )
    assert "gen_ai.request.temperature" not in out
    assert out["llm.invocation_parameters"] == blob  # untouched, nothing lost


# ---------------------------------------------------------------------------
# End-to-end ingest of framework-shaped payloads
# ---------------------------------------------------------------------------


def _payload(spans: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "resourceSpans": [
            {"scopeSpans": [{"spans": spans}]},
        ]
    }


def _span(name: str, attrs: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "traceId": "0af7651916cd43dd8448eb211c80319c",
        "spanId": "b7ad6b7169203331",
        "startTimeUnixNano": "1700000000000000000",
        "endTimeUnixNano": "1700000001000000000",
        "attributes": [{"key": k, "value": {"stringValue": str(v)}} for k, v in attrs.items()],
    }


def test_langchain_shaped_trace_now_ingests() -> None:
    """The regression this closes: these spans used to be dropped entirely."""
    result = ingest_otlp_json(
        _payload(
            [
                _span("ChatOpenAI", {
                    "openinference.span.kind": "LLM",
                    "llm.model_name": "gpt-4o-mini",
                    "llm.provider": "openai",
                }),
                _span("search_web", {
                    "openinference.span.kind": "TOOL",
                    "tool.name": "search_web",
                }),
            ]
        )
    )
    assert result.unclassified_spans == 0
    assert len(result.model_calls) == 1
    assert len(result.tool_calls) == 1
    assert result.model_calls[0]["gen_ai.request.model"] == "gpt-4o-mini"
    assert result.tool_calls[0]["tool_name"] == "search_web"


def test_crewai_agent_span_supplies_manifest_metadata() -> None:
    result = ingest_otlp_json(
        _payload([
            _span("researcher", {
                "openinference.span.kind": "AGENT",
                "graph.node.name": "researcher",
                "llm.provider": "anthropic",
            }),
        ])
    )
    assert result.agent is not None
    assert result.agent["agent_name"] == "researcher"
    assert result.agent["provider"] == "anthropic"


def test_retriever_span_becomes_a_model_call_not_a_dropped_span() -> None:
    """No retrieval primitive exists in the capsule schema; do not invent one."""
    result = ingest_otlp_json(
        _payload([
            _span("VectorStoreRetriever", {
                "openinference.span.kind": "RETRIEVER",
                "llm.model_name": "text-embedding-3-small",
            }),
        ])
    )
    assert result.unclassified_spans == 0
    assert len(result.model_calls) == 1


def test_unrelated_spans_are_still_skipped_not_ingested() -> None:
    result = ingest_otlp_json(
        _payload([_span("GET /healthz", {"http.method": "GET"})])
    )
    assert result.skipped_spans == 1
    assert not result.model_calls and not result.tool_calls
