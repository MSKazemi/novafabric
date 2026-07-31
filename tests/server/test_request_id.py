"""Request-ID correlation + JSON logging (P1 operability)."""

from __future__ import annotations

import json
import logging

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server.request_id import (  # noqa: E402
    REQUEST_ID_HEADER,
    JsonLogFormatter,
    RequestIdFilter,
    configure_logging,
    current_request_id,
    install_request_id_middleware,
    request_id_var,
    sanitize_request_id,
)


def _app() -> FastAPI:
    app = FastAPI()
    install_request_id_middleware(app)

    @app.get("/echo")
    def echo() -> dict[str, str]:
        return {"rid": current_request_id()}

    return app


def test_generates_request_id_when_absent() -> None:
    client = TestClient(_app())
    resp = client.get("/echo")
    rid = resp.headers.get(REQUEST_ID_HEADER)
    assert rid and len(rid) == 32  # uuid4 hex
    assert resp.json()["rid"] == rid  # same id visible inside the handler


def test_preserves_valid_inbound_request_id() -> None:
    client = TestClient(_app())
    resp = client.get("/echo", headers={REQUEST_ID_HEADER: "trace-abc_123"})
    assert resp.headers[REQUEST_ID_HEADER] == "trace-abc_123"
    assert resp.json()["rid"] == "trace-abc_123"


def test_replaces_invalid_inbound_request_id() -> None:
    client = TestClient(_app())
    # Contains spaces / illegal chars → replaced with a generated id.
    bad = "evil id\nwith newline"
    resp = client.get("/echo", headers={REQUEST_ID_HEADER: bad})
    assert resp.headers[REQUEST_ID_HEADER] != bad
    assert len(resp.headers[REQUEST_ID_HEADER]) == 32


def test_context_var_cleared_after_request() -> None:
    client = TestClient(_app())
    client.get("/echo")
    assert current_request_id() == ""  # reset outside the request


def test_sanitize_request_id() -> None:
    assert sanitize_request_id("ok-id_1.2") == "ok-id_1.2"
    assert len(sanitize_request_id(None)) == 32
    assert len(sanitize_request_id("has space")) == 32
    assert len(sanitize_request_id("x" * 200)) == 32  # too long → regenerated


def test_json_formatter_includes_request_id() -> None:
    token = request_id_var.set("rid-xyz")
    try:
        record = logging.LogRecord(
            "novafabric.server", logging.INFO, __file__, 1, "hello", None, None
        )
        RequestIdFilter().filter(record)
        line = JsonLogFormatter().format(record)
    finally:
        request_id_var.reset(token)
    obj = json.loads(line)
    assert obj["message"] == "hello"
    assert obj["level"] == "INFO"
    assert obj["request_id"] == "rid-xyz"


def test_configure_logging_json_sets_formatter() -> None:
    configure_logging("json")
    root = logging.getLogger()
    try:
        assert any(
            isinstance(h.formatter, JsonLogFormatter) for h in root.handlers
        )
        assert any(
            any(isinstance(f, RequestIdFilter) for f in h.filters)
            for h in root.handlers
        )
    finally:
        configure_logging("text")  # restore for other tests
