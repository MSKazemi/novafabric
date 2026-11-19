"""ADR-0320: the httpx wire hook records Ollama responses without reading ahead.

Unit level, over ``httpx.MockTransport`` (no network): the streamed body is pulled
only by the workload's own read, the record is written when that read ends, and
failures leave a record that holds its queue position (an ``error`` block).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.hooks._httpx import HttpxHook

RUN_ID = "01KOLLAMAWIRE0000000000000"
URL = "http://127.0.0.1:11434/api/chat"
CHUNKS = [
    {"model": "m", "created_at": "t", "message": {"role": "assistant", "content": "Hel"},
     "done": False},
    {"model": "m", "created_at": "t", "message": {"role": "assistant", "content": "lo"},
     "done": False},
    {"model": "m", "created_at": "t", "message": {"role": "assistant", "content": ""},
     "done": True, "done_reason": "stop", "prompt_eval_count": 4, "eval_count": 2},
]


@pytest.fixture
def hook(tmp_path: Path) -> Iterator[HttpxHook]:
    writer = CapsuleWriter(run_id=RUN_ID, base_dir=tmp_path)
    writer.open()
    h = HttpxHook(writer=writer, parent_span_id="0" * 16)
    h.install()
    try:
        yield h
    finally:
        h.uninstall()


def _records(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / RUN_ID / "model-calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _lines(objects: list[dict[str, Any]]) -> list[bytes]:
    return [json.dumps(o).encode() + b"\n" for o in objects]


class _Pulled:
    """A streamed body that counts how many chunks were pulled from it."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.pulled = 0

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.pulled += 1
            yield chunk

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.pulled += 1
            yield chunk


def _client(body: _Pulled, status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, stream=_sync(body))

    return httpx.Client(transport=httpx.MockTransport(handler))


def _sync(body: _Pulled) -> httpx.SyncByteStream:
    class S(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            return iter(body)

    return S()


def test_a_streamed_body_is_never_read_ahead_and_is_recorded_when_read(
    hook: HttpxHook, tmp_path: Path
) -> None:
    body = _Pulled(_lines(CHUNKS))
    with _client(body) as client:
        request = client.build_request("POST", URL, json={"model": "m", "messages": []})
        response = client.send(request, stream=True)
        assert body.pulled == 0, "the hook read the workload's stream"
        assert _records(tmp_path) == [], "recorded before the workload read the body"
        lines = [json.loads(line) for line in response.iter_lines() if line]
        response.close()
    assert lines == CHUNKS
    (record,) = _records(tmp_path)
    assert record["gen_ai.response.choices"] == [
        {"index": 0, "message": {"role": "assistant", "content": "Hello"},
         "finish_reason": "stop"}
    ]
    assert record["gen_ai.usage.input_tokens"] == 4
    assert record["gen_ai.usage.output_tokens"] == 2
    assert record["nova.streaming"]["chunk_count"] == 3
    assert record["extensions"]["io.novafabric.api_surface"] == "ollama.chat"
    assert record["extensions"]["io.novafabric.ollama.chunks"] == [[3, 0, 0], [2, 0, 0], [0, 0, 0]]
    assert "io.novafabric.stream_complete" not in record["extensions"]


def test_an_async_streamed_body_is_recorded(tmp_path: Path) -> None:
    writer = CapsuleWriter(run_id=RUN_ID, base_dir=tmp_path)
    writer.open()
    h = HttpxHook(writer=writer, parent_span_id="0" * 16)
    h.install()
    body = _Pulled(_lines(CHUNKS))

    class A(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            async for chunk in body:
                yield chunk

    async def go() -> list[str]:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=A())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async with client.stream("POST", URL, json={"model": "m", "messages": []}) as r:
                assert body.pulled == 0
                return [line async for line in r.aiter_lines() if line]

    try:
        lines = asyncio.run(go())
    finally:
        h.uninstall()
    assert len(lines) == 3
    (record,) = _records(tmp_path)
    assert record["gen_ai.response.choices"][0]["message"]["content"] == "Hello"


def test_an_in_stream_error_line_makes_the_record_an_error(
    hook: HttpxHook, tmp_path: Path
) -> None:
    body = _Pulled(_lines([CHUNKS[0], {"error": "model runner crashed"}]))
    with _client(body) as client:
        with client.stream("POST", URL, json={"model": "m", "messages": []}) as r:
            list(r.iter_lines())
    (record,) = _records(tmp_path)
    assert record["status"] == "error"
    assert record["error"]["type"] == "OllamaResponseError"
    assert record["error"]["message"] == "model runner crashed"
    assert record["gen_ai.response.choices"][0]["finish_reason"] is None


def test_an_http_error_holds_its_position_with_an_error_block(
    hook: HttpxHook, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "model 'm' not found"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert client.post(URL, json={"model": "m", "stream": False}).status_code == 404
    (record,) = _records(tmp_path)
    assert record["status"] == "error"
    assert record["error"]["type"] == "HTTPStatusError"
    assert record["gen_ai.response.choices"] == []
    assert record["extensions"]["io.novafabric.response_not_recorded"] == "HTTP status 404"


def test_a_body_that_is_not_json_records_why(hook: HttpxHook, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>proxy error</html>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert client.post(URL, json={"model": "m", "stream": False}).text.startswith("<html>")
    (record,) = _records(tmp_path)
    assert record["gen_ai.response.choices"] == []
    assert "not JSON" in record["extensions"]["io.novafabric.response_not_recorded"]


def test_other_ollama_endpoints_keep_the_request_only_record(
    hook: HttpxHook, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        client.get("http://127.0.0.1:11434/api/tags")
    (record,) = _records(tmp_path)
    assert record["gen_ai.system"] == "ollama"
    assert record["gen_ai.response.choices"] == []
    assert "io.novafabric.api_surface" not in record.get("extensions", {})
