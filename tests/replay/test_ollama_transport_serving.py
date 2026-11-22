"""ADR-0320: the httpx-transport Ollama surface of ``MockModelDispatcher``, unit level.

No network: every request below is answered by the patched transport (a host that
nothing listens on would raise ``ConnectError`` if it were not).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from novafabric.replay._contract import ReplayEventLog, read_events, summarize
from novafabric.replay._dispatcher import MockModelDispatcher
from novafabric.replay._errors import (
    ReplayOrderMismatchError,
    ReplayRecordedErrorUnreconstructableError,
    ReplayRecordedModelError,
)
from novafabric.replay._ollama_serve import ollama_response_objects, request_queue

#: TEST-NET-1 (RFC 5737): unroutable, so a request that is not served fails fast.
HOST = "http://192.0.2.1:11434"


def _record(surface: str, text: str, **extra: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "model_call_id": f"id-{text}",
        "gen_ai.system": "ollama",
        "gen_ai.request.model": "m",
        "gen_ai.response.model": "m",
        "gen_ai.response.choices": [{"index": 0, "message": {
            "role": "assistant", "content": text}, "finish_reason": "stop"}],
        "gen_ai.usage.input_tokens": 1,
        "gen_ai.usage.output_tokens": 2,
        "status": "success",
        "endpoint": f"{HOST}/api/{surface.split('.')[1]}",
        "extensions": {
            "io.novafabric.api_surface": surface,
            "io.novafabric.record_role": "logical",
            "io.novafabric.ollama.final": {"done_reason": "stop", "eval_count": 2},
        },
    }
    record.update(extra)
    return record


@pytest.fixture
def events(tmp_path: Path) -> Path:
    return tmp_path / "events.jsonl"


@contextmanager
def _install(records: list[dict[str, Any]], events: Path, policy: str = "fail") -> Iterator[None]:
    dispatcher = MockModelDispatcher(
        records, divergence_policy=policy, events=ReplayEventLog(events)
    )
    dispatcher.install()
    try:
        yield
    finally:
        dispatcher.uninstall()


def test_served_in_order_counted_and_never_on_the_network(events: Path) -> None:
    records = [_record("ollama.chat", "one"), _record("ollama.generate", "two")]
    with _install(records, events):
        chat = httpx.post(f"{HOST}/api/chat", json={"model": "m", "stream": False})
        with httpx.stream("POST", f"{HOST}/api/generate", json={"model": "m"}) as r:
            lines = [json.loads(line) for line in r.iter_lines() if line]
    assert chat.json()["message"] == {"role": "assistant", "content": "one"}
    assert chat.json()["done"] is True and chat.json()["eval_count"] == 2
    assert r.headers["content-type"] == "application/x-ndjson"
    assert [line["response"] for line in lines] == ["two", ""]
    assert [line["done"] for line in lines] == [False, True]
    report = summarize(records, [], read_events(events), divergence_policy="fail",
                       substitute_tools=True)
    assert (report.model_calls_mocked, report.model_calls_available) == (2, 2)
    assert report.model_calls_unconsumed == 0 and not report.divergences


def test_a_recorded_failure_is_refused_in_order_then_the_next_is_served(events: Path) -> None:
    failed = _record("ollama.chat", "x", status="error", **{
        "gen_ai.response.choices": [],
        "error": {"type": "HTTPStatusError", "message": "HTTP 500", "traceback_ref": None},
    })
    records = [failed, _record("ollama.chat", "after")]
    # Strict: refused, and the replay stays refused at that position.
    with _install(records, events):
        for _ in range(2):
            with pytest.raises(ReplayRecordedErrorUnreconstructableError, match="ADR-0320"):
                httpx.post(f"{HOST}/api/chat", json={"model": "m", "stream": False})
    # Permissive: a stand-in error at the failed call's position, then the next answer.
    with _install(records, events, policy="warn"):
        with pytest.raises(ReplayRecordedModelError):
            httpx.post(f"{HOST}/api/chat", json={"model": "m", "stream": False})
        ok = httpx.post(f"{HOST}/api/chat", json={"model": "m", "stream": False})
    assert ok.json()["message"]["content"] == "after"


def test_async_and_an_order_mismatch(events: Path) -> None:
    import asyncio

    records = [_record("ollama.chat", "one"), _record("ollama.generate", "two")]

    async def go() -> None:
        async with httpx.AsyncClient() as client:
            await client.post(f"{HOST}/api/generate", json={"model": "m", "stream": False})

    with _install(records, events):
        with pytest.raises(ReplayOrderMismatchError):
            asyncio.run(go())


def test_request_classification() -> None:
    hosts = frozenset({"10.0.0.5:7777"})
    assert request_queue("POST", f"{HOST}/api/chat", "ollama", frozenset()) == "ollama.chat"
    assert request_queue("POST", f"{HOST}/api/generate/", "ollama", frozenset()) == (
        "ollama.generate"
    )
    assert request_queue("GET", f"{HOST}/api/chat", "ollama", frozenset()) is None
    assert request_queue("POST", f"{HOST}/api/embed", "ollama", frozenset()) is None
    assert request_queue("POST", f"{HOST}/v1/chat/completions", "ollama", frozenset()) is None
    # Not in the registry at replay, but recorded: still served.
    assert request_queue("POST", "http://10.0.0.5:7777/api/chat", "unknown", hosts) == (
        "ollama.chat"
    )
    assert request_queue("POST", "http://10.0.0.6:7777/api/chat", "unknown", hosts) is None


def test_tool_calls_are_served_in_ollama_shape_without_an_invented_id() -> None:
    record = _record("ollama.chat", "")
    record["gen_ai.response.choices"][0]["message"]["tool_calls"] = [
        {"id": "", "name": "f", "arguments": {"a": 1}},
        {"id": "call_1", "name": "g", "arguments": {}},
    ]
    (obj,) = ollama_response_objects("ollama.chat", record, stream=False)
    assert obj["message"]["tool_calls"] == [
        {"function": {"name": "f", "arguments": {"a": 1}}},
        {"id": "call_1", "function": {"name": "g", "arguments": {}}},
    ]
