"""Sink behavior: local log, webhook delivery, bounded retry, fail-safe (ADR-0137 D3/D4)."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from novafabric.events.signing import SIGNATURE_HEADER, sign_record
from novafabric.events.sinks import FileSink, MultiSink, NullSink, WebhookSink

RECORD: dict[str, Any] = {
    "schema_version": "0.1.0",
    "event_id": "01J8ZQK7M7QM4YZ2K7N9DPBYK2",
    "type": "capsule.created",
    "subject": {"kind": "capsule", "ref": "run-abc", "digest": None},
    "occurred_at": "2026-07-15T00:00:00.000000Z",
    "payload": {"status": "success"},
}


def _free_closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TestNullAndFileSinks:
    def test_null_sink_is_noop(self) -> None:
        NullSink().send(RECORD)  # must not raise, must not write anything

    def test_file_sink_appends_json_lines(self, tmp_path: Path) -> None:
        sink = FileSink(tmp_path / "sub" / "events.jsonl")
        sink.send(RECORD)
        sink.send(RECORD)
        lines = (tmp_path / "sub" / "events.jsonl").read_text().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0]) == RECORD


class TestWebhookSink:
    def test_webhook_fires(self, webhook_server: Any) -> None:
        WebhookSink(webhook_server.url, backoff_s=0.0).send(RECORD)
        assert len(webhook_server.received) == 1
        request = webhook_server.received[0]
        assert json.loads(request["body"]) == RECORD
        assert request["headers"]["Content-Type"] == "application/json"
        assert SIGNATURE_HEADER not in request["headers"]

    def test_signature_header_sent_for_signed_record(self, webhook_server: Any) -> None:
        signed = sign_record(RECORD, b"s3cret", "ci")
        WebhookSink(webhook_server.url, backoff_s=0.0).send(signed)
        request = webhook_server.received[0]
        assert request["headers"][SIGNATURE_HEADER] == signed["signature"]["value"]

    def test_retries_are_bounded_and_never_raise(self, webhook_server: Any) -> None:
        webhook_server.server.response_code = 500
        sink = WebhookSink(webhook_server.url, max_retries=2, backoff_s=0.0)
        sink.send(RECORD)  # must not raise
        # exactly max_retries + 1 attempts, then give up — no unbounded queue
        assert len(webhook_server.received) == 3

    def test_zero_retries_means_single_attempt(self, webhook_server: Any) -> None:
        webhook_server.server.response_code = 503
        WebhookSink(webhook_server.url, max_retries=0, backoff_s=0.0).send(RECORD)
        assert len(webhook_server.received) == 1

    def test_unreachable_endpoint_never_raises(self) -> None:
        url = f"http://127.0.0.1:{_free_closed_port()}/hook"
        WebhookSink(url, max_retries=1, backoff_s=0.0, timeout_s=0.5).send(RECORD)


class TestMultiSink:
    def test_fans_out_and_isolates_failures(self, tmp_path: Path) -> None:
        class BoomSink:
            def send(self, record: dict[str, Any]) -> None:
                raise RuntimeError("boom")

        log = tmp_path / "events.jsonl"
        multi = MultiSink([BoomSink(), FileSink(log)])
        multi.send(RECORD)  # must not raise; file sink still delivered
        assert len(log.read_text().splitlines()) == 1
