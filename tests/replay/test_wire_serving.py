"""ADR-0324 unit level: the generic-wire match key, the body sink, precedence at
capture, and the serve-time checks that treat a capsule as untrusted data."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from novafabric.capture.hooks._wire_body import (
    MAX_WIRE_BODY_CHARS,
    MAX_WIRE_CHUNKS,
    WIRE_REQUEST_EXT,
    WIRE_RESPONSE_EXT,
    WireBodySink,
    allowed_headers,
    request_key,
)
from novafabric.replay._contract import model_queue_key, model_queues
from novafabric.replay._servability import METADATA, NOT_SERVABLE, SERVABLE, classify
from novafabric.replay._wire_serve import request_mismatch, served_response, unservable_reason

URL = "https://api.mistral.ai/v1/chat/completions"


def _record(**response: Any) -> dict[str, Any]:
    wire = {"v": 1, "status": 200, "reason": "OK", "headers": [["content-type", "x/y"]],
            "streamed": False, "complete": True, "bytes": 2, "chunks": ["{}"]}
    wire.update(response)
    return {
        "model_call_id": "m1",
        "gen_ai.system": "mistral",
        "gen_ai.response.choices": [],
        "status": "success",
        "endpoint": URL,
        "extensions": {
            "io.novafabric.record_role": "logical",
            "io.novafabric.api_surface": "generic-wire",
            WIRE_REQUEST_EXT: request_key("POST", URL, b'{"a":1}'),
            WIRE_RESPONSE_EXT: wire,
        },
    }


# ── the match key ────────────────────────────────────────────────────────────


def test_the_digest_ignores_key_order_whitespace_volatile_keys_and_credentials() -> None:
    a = request_key("post", URL + "?key=one&alt=sse", b'{"model":"m","messages":[1],"nonce":1}')
    b = request_key(
        "POST", URL + "?alt=sse&key=two",
        b'{ "messages": [1],  "model": "m", "request_id": "x" }',
    )
    assert a == b
    assert a["method"] == "POST" and a["body"] == "json"


@pytest.mark.parametrize("other", [
    ("POST", URL, b'{"model":"m","messages":[2]}'),          # another question
    ("POST", URL + "?alt=json", b'{"model":"m","messages":[1]}'),  # another query
    ("POST", URL, b"not json"),
])
def test_the_digest_changes_with_the_question(other: tuple[str, str, bytes]) -> None:
    base = request_key("POST", URL, b'{"model":"m","messages":[1]}')
    assert request_key(*other)["digest"] != base["digest"]


def test_a_mismatch_names_what_differs_and_never_the_text() -> None:
    record = _record()
    assert request_mismatch(record, method="POST", url=URL, body=b'{"a": 1}',
                            system="mistral") is None
    differs = request_mismatch(record, method="POST", url=URL, body=b'{"a":"SECRET Q"}',
                               system="mistral")
    assert differs is not None and differs["differs"] == ["digest"]
    assert "SECRET" not in json.dumps(differs)
    other = request_mismatch(record, method="GET", url=URL.replace("chat", "x"), body=None,
                             system="cohere")
    assert other is not None
    assert other["differs"] == ["digest", "method", "path", "system"]


# ── the body sink ────────────────────────────────────────────────────────────


def test_a_character_split_across_network_chunks_is_kept_whole() -> None:
    sink = WireBodySink()
    data = "data: é\n\n".encode()
    cut = data.index(b"\xa9")  # inside the two-byte é
    sink.add(data[:cut])
    sink.add(data[cut:])
    sink.close()
    assert sink.not_recorded is None
    assert "".join(sink.chunks) == "data: é\n\n"


@pytest.mark.parametrize(("encoding", "chunks", "why"), [
    ("gzip", [gzip.compress(b"{}")], "content-encoding"),
    ("", [b"\xff\xfe\x00"], "not UTF-8"),
    ("", [b"x" * (MAX_WIRE_BODY_CHARS + 1)], "character cap"),
    ("", [b"x"] * (MAX_WIRE_CHUNKS + 1), "chunk cap"),
])
def test_a_body_that_cannot_be_kept_faithfully_is_dropped_with_a_reason(
    encoding: str, chunks: list[bytes], why: str
) -> None:
    sink = WireBodySink(encoding=encoding)
    for chunk in chunks:
        sink.add(chunk)
    sink.close()
    assert sink.not_recorded is not None and why in sink.not_recorded
    assert sink.chunks == []


def test_the_header_allowlist_drops_cookies_redirects_auth_and_framing() -> None:
    kept = allowed_headers([
        ("Content-Type", "application/json"), ("Set-Cookie", "s=1"), ("Location", "http://x"),
        ("WWW-Authenticate", "Bearer"), ("Content-Encoding", "gzip"), ("Content-Length", "9"),
        ("Transfer-Encoding", "chunked"), ("Retry-After", "1"), ("X-RateLimit-Remaining", "3"),
        ("Authorization", "Bearer t"), (b"x-request-id", b"r1"), ("bad",), 7,
    ])
    assert kept == [["content-type", "application/json"], ["retry-after", "1"],
                    ["x-ratelimit-remaining", "3"], ["x-request-id", "r1"]]


# ── serving an untrusted record ──────────────────────────────────────────────


def test_a_served_record_never_carries_a_header_outside_the_allowlist() -> None:
    record = _record(headers=[["set-cookie", "planted=1"], ["location", "http://evil"],
                              ["content-length", "999"], ["content-type", "text/plain"]],
                     chunks=["ab", "c"])
    assert unservable_reason(record) is None
    served = served_response(record)
    assert served.headers == [("content-type", "text/plain")]
    assert served.framed_headers() == [("content-type", "text/plain"), ("content-length", "3")]
    assert served.chunks == [b"ab", b"c"]


@pytest.mark.parametrize(("change", "why"), [
    ({"status": 700}, "HTTP status"),
    ({"status": "200"}, "HTTP status"),
    ({"status": True}, "HTTP status"),
    ({"chunks": [1, 2]}, "text chunks"),
    ({"chunks": "body"}, "text chunks"),
    ({"chunks": ["x"] * (MAX_WIRE_CHUNKS + 1)}, "chunks"),
    ({"chunks": ["x" * (MAX_WIRE_BODY_CHARS + 1)]}, "exceeds"),
    ({"headers": "content-type: x"}, "headers"),
])
def test_a_malformed_record_is_refused(change: dict[str, Any], why: str) -> None:
    reason = unservable_reason(_record(**change))
    assert reason is not None and why in reason


# ── classification ───────────────────────────────────────────────────────────


def test_a_generic_wire_record_with_a_response_is_servable_and_queued() -> None:
    with_body = _record()
    without = _record()
    del without["extensions"][WIRE_RESPONSE_EXT]
    throttled = _record(status=429)
    throttled["status"] = "error"
    as_openai = _record()
    as_openai["gen_ai.system"] = "openai"  # raw httpx to api.openai.com: no SDK scope
    assert classify([with_body, without, throttled, as_openai]) == [
        SERVABLE, NOT_SERVABLE, SERVABLE, SERVABLE
    ]
    assert model_queue_key(as_openai) == "generic-wire"
    assert len(model_queues([with_body, without, throttled, as_openai])["generic-wire"]) == 3


def test_an_ollama_metadata_call_recorded_without_a_body_stays_metadata() -> None:
    tags = _record()
    tags["gen_ai.system"] = "ollama"
    tags["endpoint"] = "http://127.0.0.1:11434/api/tags"
    del tags["extensions"][WIRE_RESPONSE_EXT]
    assert classify([tags]) == [METADATA]


# ── precedence at capture (ADR-0324 D6) ──────────────────────────────────────


def _records(tmp_path: Path) -> list[dict[str, Any]]:
    path = next(tmp_path.rglob("model-calls.jsonl"))
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture
def hooked(tmp_path: Path) -> Any:
    from novafabric.capture.capsule import CapsuleWriter
    from novafabric.capture.hooks._httpx import HttpxHook

    writer = CapsuleWriter(run_id="01HZZZZZZZZZZZZZZZZZZZZZZZ", base_dir=tmp_path)
    writer.open()
    hook = HttpxHook(writer=writer, parent_span_id="0" * 16)
    hook.install()
    yield hook
    hook.uninstall()


def _client() -> httpx.Client:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True}, headers={"set-cookie": "s=1"})

    return httpx.Client(transport=httpx.MockTransport(answer))


def test_capture_stores_a_body_only_for_a_logical_call_no_dedicated_server_claims(
    hooked: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.capture.record_roles import sdk_call_scope

    for var in ("OLLAMA_BASE_URL", "OLLAMA_HOST", "NOVAFABRIC_CAPTURE_WIRE_RESPONSES"):
        monkeypatch.delenv(var, raising=False)
    with _client() as client:
        client.post(URL, json={"q": 1})  # logical, registry host: stored
        with sdk_call_scope("01SDKCALL"):  # beneath an SDK call: the SDK hook records it
            client.post(URL, json={"q": 2})
        client.post("http://127.0.0.1:11434/api/chat", json={"stream": False})  # Ollama: ADR-0320
        client.post("https://example.com/v1/chat", json={})  # not a registry host: no record
    first, transport, ollama = _records(tmp_path)
    assert first["extensions"]["io.novafabric.api_surface"] == "generic-wire"
    wire = first["extensions"][WIRE_RESPONSE_EXT]
    assert json.loads("".join(wire["chunks"])) == {"ok": True}
    assert dict(wire["headers"]) == {"content-type": "application/json"}
    assert transport["extensions"]["io.novafabric.record_role"] == "transport"
    assert WIRE_RESPONSE_EXT not in transport["extensions"]
    assert "io.novafabric.api_surface" not in transport["extensions"]
    assert ollama["extensions"]["io.novafabric.api_surface"] == "ollama.chat"
    assert WIRE_RESPONSE_EXT not in ollama["extensions"]
