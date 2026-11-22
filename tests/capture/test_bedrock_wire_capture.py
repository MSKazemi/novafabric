"""The urllib3 wire hook records AWS Bedrock Converse responses without reading ahead.

Unit level, in-process: a stdlib fake Bedrock server on 127.0.0.1 (never AWS), the
hook installed over a real ``CapsuleWriter``, and real ``urllib3`` / ``boto3`` clients.
The body is pulled only by the client's own read, the record is written when that
read ends, and the SigV4 headers never reach the record.
"""

from __future__ import annotations

import gzip
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.hooks._aws_eventstream import encode_message
from novafabric.capture.hooks._url_registry import load_url_registry
from novafabric.capture.hooks._urllib3 import Urllib3Hook

urllib3 = pytest.importorskip("urllib3")

RUN_ID = "01KBEDROCKWIRE000000000000"
REPO = Path(__file__).resolve().parents[2]
_SCHEMA = json.loads((REPO / "src/novafabric/schemas/model-call.schema.json").read_text())
CONVERSE = {
    "output": {"message": {"role": "assistant", "content": [
        {"text": "Hel"}, {"toolUse": {"toolUseId": "t1", "name": "f", "input": {"a": 1}}},
        {"text": "lo"},
    ]}},
    "stopReason": "tool_use", "usage": {"inputTokens": 5, "outputTokens": 2, "totalTokens": 7},
    "metrics": {"latencyMs": 9},
}


def _event(kind: str, payload: dict[str, Any]) -> bytes:
    return encode_message([(":event-type", kind), (":content-type", "application/json"),
                           (":message-type", "event")], json.dumps(payload).encode())


STREAM = b"".join([
    _event("messageStart", {"role": "assistant", "p": "xyz"}),
    _event("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "Hi "}}),
    _event("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "there"}}),
    _event("contentBlockStop", {"contentBlockIndex": 0}),
    _event("messageStop", {"stopReason": "max_tokens"}),
    _event("metadata", {"usage": {"inputTokens": 3, "outputTokens": 4, "totalTokens": 7},
                        "metrics": {"latencyMs": 1}}),
])
EXCEPTION_STREAM = _event("messageStart", {"role": "assistant"}) + encode_message(
    [(":exception-type", "modelStreamErrorException"), (":content-type", "application/json"),
     (":message-type", "exception")],
    json.dumps({"message": "the model failed", "originalStatusCode": 500}).encode(),
)
REASONING_STREAM = b"".join([
    _event("messageStart", {"role": "assistant"}),
    _event("contentBlockDelta", {"contentBlockIndex": 0, "delta": {
        "reasoningContent": {"text": "thinking"}}}),
    _event("contentBlockStop", {"contentBlockIndex": 0}),
    _event("contentBlockDelta", {"contentBlockIndex": 1, "delta": {"text": "ok"}}),
    encode_message([(":event-type", "futureEvent"), (":message-type", "event")], b"not json"),
    _event("messageStop", {"stopReason": "end_turn"}),
])
SEEN: list[dict[str, str]] = []


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 -- http.server API
        self.rfile.read(int(self.headers.get("content-length", 0)))
        SEEN.append({k.lower(): v for k, v in self.headers.items()})
        status = 200
        if "converse-stream" in self.path:
            data, ctype = STREAM, "application/vnd.amazon.eventstream"
            if "exception" in self.path:
                data = EXCEPTION_STREAM
            elif "reasoning" in self.path:
                data = REASONING_STREAM
        else:
            data, ctype = json.dumps(CONVERSE).encode(), "application/json"
        extra = []
        if "throttle" in self.path:
            status, data = 429, b'{"message":"Too many requests"}'
            extra.append(("x-amzn-ErrorType", "ThrottlingException:http://internal/"))
        if "gzip" in self.path:
            data = gzip.compress(data)
            extra.append(("content-encoding", "gzip"))
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("x-amzn-RequestId", "rid-1")
        for k, v in extra:
            self.send_header(k, v)
        if "chunked" in self.path:
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            for at in range(0, len(data), 50):
                piece = data[at:at + 50]
                self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture(scope="module")
def base() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


@pytest.fixture
def hook(
    tmp_path: Path, base: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Urllib3Hook]:
    monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK_RUNTIME", base)
    writer = CapsuleWriter(run_id=RUN_ID, base_dir=tmp_path)
    writer.open()
    h = Urllib3Hook(writer=writer, parent_span_id="0" * 16)
    h.install()
    try:
        yield h
    finally:
        h.uninstall()


def _validate(record: dict[str, Any]) -> None:
    """The record against the in-force schema -- except ``gen_ai.request.messages``,
    which holds Bedrock-native content blocks (``{"text": ...}``, ``{"toolUse": ...}``)
    verbatim, as before this change and as Anthropic's native blocks are kept; the
    schema's ``ContentPart`` does not describe provider-native blocks."""
    jsonschema.validate({**record, "gen_ai.request.messages": []}, _SCHEMA)


def _records(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / RUN_ID / "model-calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _post(base: str, path: str, **kwargs: Any) -> Any:
    pool = urllib3.PoolManager()
    body = json.dumps({"messages": [{"role": "user", "content": [{"text": "q"}]}],
                       "inferenceConfig": {"maxTokens": 8}}).encode()
    return pool.request("POST", base + path, body=body,
                        headers={"Authorization": "AWS4-HMAC-SHA256 Credential=AKIDSECRET/x",
                                 "X-Amz-Date": "20261120T000000Z"}, **kwargs)


def test_the_endpoint_variable_classifies_the_host_at_call_time(
    base: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = load_url_registry()
    url = base + "/model/m/converse"
    monkeypatch.delenv("AWS_ENDPOINT_URL_BEDROCK_RUNTIME", raising=False)
    monkeypatch.delenv("AWS_ENDPOINT_URL_BEDROCK", raising=False)
    assert registry.gen_ai_system(url) == "unknown"
    monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK_RUNTIME", base + "/")
    assert registry.gen_ai_system(url) == "aws.bedrock"
    # Exactly that host:port -- not another port on the same host.
    assert registry.gen_ai_system("http://127.0.0.1:1/model/m/converse") == "unknown"
    assert registry.gen_ai_system(
        "https://bedrock-runtime.eu-central-1.amazonaws.com/model/m/converse"
    ) == "aws.bedrock"


def test_an_unread_body_is_not_read_ahead_and_is_recorded_when_read(
    hook: Urllib3Hook, base: str, tmp_path: Path
) -> None:
    response = _post(base, "/model/qwen.qwen3-32b-v1%3A0/converse", preload_content=False)
    assert _records(tmp_path) == [], "recorded before the client read the body"
    assert "read" in vars(response), "the response's read was not shadowed"
    data = b"".join(response.stream(7))
    assert json.loads(data) == CONVERSE
    (record,) = _records(tmp_path)
    assert record["gen_ai.request.model"] == "qwen.qwen3-32b-v1:0"
    assert record["gen_ai.request.max_tokens"] == 8
    (choice,) = record["gen_ai.response.choices"]
    assert choice == {"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "Hello",
        "tool_calls": [{"id": "t1", "name": "f", "arguments": {"a": 1}}]}}
    assert record["extensions"]["io.novafabric.bedrock.blocks"] == [
        {"type": "text", "chars": 3}, {"type": "toolUse", "call": 0},
        {"type": "text", "chars": 2},
    ]
    assert record["extensions"]["io.novafabric.provider_finish_reasons"] == ["tool_use"]
    assert ["x-amzn-RequestId", "rid-1"] in record["extensions"][
        "io.novafabric.bedrock.response_headers"]
    text = json.dumps(record)
    assert "AKIDSECRET" not in text and "20261120T000000Z" not in text
    assert "authorization" in SEEN[-1], "the request was sent without its header"
    _validate(record)


def test_a_preloaded_body_is_recorded_at_once(
    hook: Urllib3Hook, base: str, tmp_path: Path
) -> None:
    response = _post(base, "/model/m/converse")
    assert json.loads(response.data) == CONVERSE
    (record,) = _records(tmp_path)
    assert record["gen_ai.response.choices"][0]["message"]["content"] == "Hello"


def test_a_stream_is_folded_frame_by_frame(
    hook: Urllib3Hook, base: str, tmp_path: Path
) -> None:
    response = _post(base, "/model/m/converse-stream", preload_content=False)
    assert b"".join(response.stream(5)) == STREAM
    (record,) = _records(tmp_path)
    (choice,) = record["gen_ai.response.choices"]
    assert choice["message"] == {"role": "assistant", "content": "Hi there"}
    assert choice["finish_reason"] == "length"
    assert record["gen_ai.usage.output_tokens"] == 4
    assert record["nova.streaming"]["chunk_count"] == 6
    events = record["extensions"]["io.novafabric.bedrock.events"]
    assert events[:3] == [["messageStart", {"role": "assistant"}],
                          ["delta", 0, "text", 3], ["delta", 0, "text", 5]]
    assert "xyz" not in json.dumps(record), "the padding field was recorded"
    _validate(record)


def test_a_stream_closed_early_is_flagged_incomplete(
    hook: Urllib3Hook, base: str, tmp_path: Path
) -> None:
    response = _post(base, "/model/m/converse-stream", preload_content=False)
    next(response.stream(20))
    response.close()
    (record,) = _records(tmp_path)
    assert record["extensions"]["io.novafabric.stream_complete"] is False


def test_a_json_body_closed_unread_is_recorded_without_a_response(
    hook: Urllib3Hook, base: str, tmp_path: Path
) -> None:
    response = _post(base, "/model/m/converse", preload_content=False)
    response.close()
    (record,) = _records(tmp_path)
    assert record["gen_ai.response.choices"] == []
    assert record["extensions"]["io.novafabric.response_not_recorded"] == (
        "the response body was not read to its end"
    )


def test_a_compressed_body_is_not_recorded_but_passes_through(
    hook: Urllib3Hook, base: str, tmp_path: Path
) -> None:
    response = _post(base, "/model/m/converse?gzip", preload_content=False)
    assert json.loads(gzip.decompress(b"".join(response.stream(decode_content=False)))) == (
        CONVERSE
    )
    (record,) = _records(tmp_path)
    assert record["gen_ai.response.choices"] == []
    assert "content-encoding" in record["extensions"]["io.novafabric.response_not_recorded"]


def test_boto3_converse_is_recorded_once_with_its_response(
    hook: Urllib3Hook, base: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boto3 = pytest.importorskip("boto3")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake")
    client = boto3.client("bedrock-runtime", region_name="eu-central-1")
    result = client.converse(modelId="qwen.qwen3-32b-v1:0",
                             messages=[{"role": "user", "content": [{"text": "q"}]}])
    assert result["output"] == CONVERSE["output"]
    stream = client.converse_stream(modelId="m",
                                    messages=[{"role": "user", "content": [{"text": "q"}]}])
    events = list(stream["stream"])
    assert events[1] == {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Hi "}}}
    first, second = _records(tmp_path)
    assert first["gen_ai.request.model"] == "qwen.qwen3-32b-v1:0"
    assert first["gen_ai.response.choices"][0]["message"]["tool_calls"][0]["id"] == "t1"
    assert second["gen_ai.response.choices"][0]["message"]["content"] == "Hi there"
    assert SEEN[-1]["authorization"].startswith("AWS4-HMAC-SHA256")
    assert "AKIDEXAMPLE" not in json.dumps([first, second])


def test_a_record_after_the_hook_is_gone_is_dropped(
    hook: Urllib3Hook, base: str, tmp_path: Path
) -> None:
    response = _post(base, "/model/m/converse-stream", preload_content=False)
    hook.uninstall()
    b"".join(response.stream())
    assert _records(tmp_path) == []


def test_a_chunked_stream_is_copied_through_read_chunked(
    hook: Urllib3Hook, base: str, tmp_path: Path
) -> None:
    response = _post(base, "/model/m/converse-stream?chunked", preload_content=False)
    assert response.chunked
    assert b"".join(response.stream(16)) == STREAM
    (record,) = _records(tmp_path)
    assert record["gen_ai.response.choices"][0]["message"]["content"] == "Hi there"
    assert "io.novafabric.stream_complete" not in record["extensions"]


def test_a_body_read_with_read1_is_recorded(
    hook: Urllib3Hook, base: str, tmp_path: Path
) -> None:
    response = _post(base, "/model/m/converse", preload_content=False)
    parts = []
    while chunk := response.read1(11):
        parts.append(chunk)
    assert json.loads(b"".join(parts)) == CONVERSE
    (record,) = _records(tmp_path)
    assert record["gen_ai.response.choices"][0]["message"]["content"] == "Hello"


def test_an_http_error_records_its_body_and_holds_its_position(
    hook: Urllib3Hook, base: str, tmp_path: Path
) -> None:
    response = _post(base, "/model/m/converse?throttle", preload_content=False)
    assert response.status == 429 and response.read() == b'{"message":"Too many requests"}'
    (record,) = _records(tmp_path)
    assert record["status"] == "error"
    assert record["error"]["type"] == "ThrottlingException"
    assert record["error"]["message"] == "Too many requests"
    assert record["extensions"]["io.novafabric.bedrock.http_error"] == {
        "status": 429, "body": '{"message":"Too many requests"}'}
    _validate(record)


def test_a_request_that_raises_holds_its_position_without_a_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    with socket.socket() as probe:  # a free port nothing listens on
        probe.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{probe.getsockname()[1]}"
    monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK_RUNTIME", dead)
    writer = CapsuleWriter(run_id=RUN_ID, base_dir=tmp_path)
    writer.open()
    h = Urllib3Hook(writer=writer, parent_span_id="0" * 16)
    h.install()
    try:
        with pytest.raises(urllib3.exceptions.HTTPError):
            urllib3.PoolManager(retries=False).request("POST", dead + "/model/m/converse",
                                                       body=b"{}")
    finally:
        h.uninstall()
    (record,) = _records(tmp_path)
    assert record["status"] == "error" and record["error"]["type"]
    assert record["extensions"]["io.novafabric.api_surface"] == "aws.bedrock.converse"
    assert record["gen_ai.response.choices"] == []


def test_an_in_stream_exception_is_recorded_as_delivered(
    hook: Urllib3Hook, base: str, tmp_path: Path
) -> None:
    response = _post(base, "/model/m/converse-stream?exception", preload_content=False)
    assert b"".join(response.stream()) == EXCEPTION_STREAM
    (record,) = _records(tmp_path)
    assert record["status"] == "error"
    assert record["error"] == {"type": "modelStreamErrorException",
                               "message": "the model failed", "traceback_ref": None}
    assert record["extensions"]["io.novafabric.bedrock.events"][-1][0] == "exception"


def test_blocks_the_canonical_message_cannot_hold_are_kept_verbatim(
    hook: Urllib3Hook, base: str, tmp_path: Path
) -> None:
    response = _post(base, "/model/m/converse-stream?reasoning", preload_content=False)
    b"".join(response.stream())
    (record,) = _records(tmp_path)
    assert record["gen_ai.response.choices"][0]["message"]["content"] == "ok"
    events = record["extensions"]["io.novafabric.bedrock.events"]
    assert ["contentBlockDelta", {"contentBlockIndex": 0, "delta": {
        "reasoningContent": {"text": "thinking"}}}] in events
    assert ["raw", {":event-type": "futureEvent", ":message-type": "event"}, "not json"] in events
    assert record["extensions"]["io.novafabric.bedrock.blocks"] == [
        {"type": "other", "index": 0}, {"type": "text", "chars": 2, "index": 1}]


def test_request_extras_too_large_to_keep_are_named_not_recorded() -> None:
    from novafabric.capture.hooks._bedrock import MAX_VERBATIM_BYTES, request_extension

    big = {"system": [{"text": "x" * (MAX_VERBATIM_BYTES + 1)}]}
    assert request_extension(big) == {"omitted": f"larger than {MAX_VERBATIM_BYTES} bytes"}
    assert request_extension({"messages": []}) is None
