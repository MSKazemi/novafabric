"""ADR-0319: Ollama calls that run no model are not refused as unservable model calls.

Real Ollama workloads list or inspect models (`/api/tags`, `/api/show`) besides
chatting; capture classifies those requests by host as `gen_ai.system: ollama`
with no response (the RQ3 capsules of paper 01 hold such records). Refusing a
replay because of them would make every such capture unreplayable, though no model
runs. `/api/embed` does run a model and stays not servable.
"""

from __future__ import annotations

from typing import Any

import pytest

from novafabric.replay._servability import (
    METADATA,
    NOT_SERVABLE,
    classify,
    not_servable_by_system,
)


def _ollama(path: str, *, port: int = 11434) -> dict[str, Any]:
    return {
        "gen_ai.system": "ollama",
        "endpoint": f"http://localhost:{port}{path}",
        "gen_ai.request.model": "unknown",
        "gen_ai.response.choices": [],
        "status": "success",
    }


@pytest.mark.parametrize(
    "path", ["/api/tags", "/api/show", "/api/version", "/api/ps", "/api/tags/"]
)
def test_an_ollama_metadata_call_is_metadata_not_unservable(path: str) -> None:
    assert classify([_ollama(path)]) == [METADATA]
    assert not_servable_by_system([_ollama(path)]) == {}


@pytest.mark.parametrize("path", ["/api/embed", "/api/embeddings", "/api/chat", "/api/generate"])
def test_an_ollama_call_that_runs_a_model_without_a_response_is_not_servable(path: str) -> None:
    assert classify([_ollama(path)]) == [NOT_SERVABLE]


def test_a_metadata_path_on_another_system_is_not_exempt() -> None:
    record = _ollama("/api/tags") | {"gen_ai.system": "huggingface"}
    assert classify([record]) == [NOT_SERVABLE]


def test_mixed_capsule_counts_only_the_model_call() -> None:
    records = [_ollama("/api/tags", port=19999), _ollama("/api/chat")]
    assert classify(records) == [METADATA, NOT_SERVABLE]
    assert not_servable_by_system(records) == {"ollama": {"count": 1, "hosts": ["localhost:11434"]}}
