from collections.abc import Generator

import opentelemetry.trace as _otel_trace_module
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from novafabric.sdk.agent import agent


@pytest.fixture
def memory_exporter() -> Generator[InMemorySpanExporter, None, None]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # OTel guards set_tracer_provider with a Once lock; reset both the pointer
    # and the Once._done flag so each test gets a clean provider.
    _otel_trace_module._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    _otel_trace_module._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    trace.set_tracer_provider(provider)
    yield exporter
    exporter.clear()
    _otel_trace_module._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    _otel_trace_module._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]


def test_agent_decorator_emits_span(memory_exporter: InMemorySpanExporter) -> None:
    @agent(name="test-agent", version="v1.0.0")
    def my_fn() -> str:
        return "result"

    result = my_fn()
    assert result == "result"

    spans = memory_exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["gen_ai.agent.name"] == "test-agent"
    assert attrs["gen_ai.agent.version"] == "v1.0.0"
    assert "gen_ai.agent.id" in attrs


def test_agent_decorator_noop_without_provider() -> None:
    # Reset to the true no-op state: _TRACER_PROVIDER=None causes get_tracer_provider()
    # to return the module-level _PROXY_TRACER_PROVIDER (no recursion, no-op spans).
    # Calling set_tracer_provider(ProxyTracerProvider()) would store a
    # ProxyTracerProvider as the global, causing infinite recursion when
    # ProxyTracerProvider.get_tracer()
    # checks the same global.
    _otel_trace_module._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    _otel_trace_module._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]

    @agent(name="noop-agent", version="v0.1.0")
    def fn() -> int:
        return 42

    result = fn()  # must not raise
    assert result == 42


def test_agent_decorator_preserves_signature() -> None:
    @agent(name="sig-agent", version="v1.0.0")
    def greet(name: str, greeting: str = "Hello") -> str:
        return f"{greeting}, {name}!"

    assert greet("Alice") == "Hello, Alice!"
    assert greet("Bob", greeting="Hi") == "Hi, Bob!"


def test_agent_decorator_propagates_exception(
    memory_exporter: InMemorySpanExporter,
) -> None:
    @agent(name="err-agent", version="v1.0.0")
    def failing() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        failing()

    spans = memory_exporter.get_finished_spans()
    assert len(spans) == 1
