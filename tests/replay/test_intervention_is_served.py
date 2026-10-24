"""``replace_model_response`` reaches what the replayed workload is served (review rlr-05).

The intervention wrote a ``response`` field the mocked-replay dispatcher never reads
— it serves ``gen_ai.response.choices`` — so a counterfactual "the model said DENY"
re-ran with the recorded "APPROVE", while the result said the substitution was
delivered. These tests check the substituted record through the dispatcher's own
response builders, for each provider queue.
"""

from __future__ import annotations

from typing import Any

import pytest

from novafabric.replay._dispatcher import build_served_response
from novafabric.replay._intervention import (
    InterventionSpec,
    InterventionSpecError,
    apply_intervention,
)


def _recorded() -> dict[str, Any]:
    return {
        "span_id": "s0",
        "gen_ai.request.model": "m",
        "gen_ai.response.model": "m",
        "gen_ai.response.choices": [
            {"index": 0, "message": {"role": "assistant", "content": "APPROVE"},
             "finish_reason": "stop"},
        ],
        "gen_ai.usage.input_tokens": 3,
        "gen_ai.usage.output_tokens": 1,
    }


def _intervene(value: Any) -> dict[str, Any]:
    spec = InterventionSpec.model_validate(
        {"target": {"stream": "model-calls", "event_index": 0}, "replace_model_response": value}
    )
    model_calls, _, _ = apply_intervention(spec, [_recorded()], [])
    return model_calls[0]


def _served_text(record: dict[str, Any], queue: str) -> str:
    served = build_served_response(queue, record, stream=False, asynchronous=False)
    if queue == "anthropic":
        return "".join(part.text for part in served.content if part.type == "text")
    if queue == "openai.responses":
        return served.output_text
    return served.choices[0].message.content


@pytest.mark.parametrize("queue", ["openai", "anthropic", "openai.responses"])
@pytest.mark.parametrize(
    "value",
    ["DENY the request", {"content": "DENY the request"},
     [{"index": 0, "message": {"role": "assistant", "content": "DENY the request"},
       "finish_reason": "stop"}]],
    ids=["text", "message", "choices"],
)
def test_the_substituted_response_is_what_the_workload_is_served(queue: str, value: Any) -> None:
    record = _intervene(value)
    assert _served_text(record, queue) == "DENY the request"
    assert "response" not in record  # no field the dispatcher would ignore


def test_a_streamed_call_serves_the_substitution_too() -> None:
    record = _intervene("DENY")
    stream = build_served_response("openai", record, stream=True, asynchronous=False)
    text = "".join(
        chunk.choices[0].delta.content or ""
        for chunk in stream
        if chunk.choices and getattr(chunk.choices[0].delta, "content", None)
    )
    assert text == "DENY"


def test_a_shape_the_dispatcher_cannot_serve_is_refused() -> None:
    with pytest.raises(InterventionSpecError):
        _intervene({"verdict": "deny"})  # neither text, a message, nor choices
