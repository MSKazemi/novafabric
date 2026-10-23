"""A wire hook must never consume, buffer or delay a streamed response body.

The ``requests`` hook sized its network event with ``len(response.content)``.
On a ``stream=True`` response ``Response.content`` *reads the whole body*, so
the hook drained the stream inside the patched ``Session.send``: a 3 s SSE
stream made ``send`` return after 3 s instead of at once, and an endless SSE
stream (a chat that never ends, a server-sent event feed) made ``send`` never
return at all. Capture changed the workload's behaviour -- ADR-0021 forbids it.

Every wire hook is driven here against a real local server that sends the
headers and a first chunk, then **holds the rest of the body** until the test
releases it (or streams forever). The call that returns the response must
return while the server is still holding the body, and the bytes the caller
then iterates must be exactly the bytes the server sent. The size recorded in
``network_events.jsonl`` comes from ``Content-Length`` when the server declared
one, and is unknown (``null``) otherwise -- never from reading the body.

The hooks are installed through :func:`install_all`, as ``nova capture`` does,
with a URL registry that matches the local server, so the requests -> urllib3
layering is exercised too. ADR-0305: each call is one logical model call.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.event_recorder import (
    EventRecorder,
    clear_current_recorder,
    set_current_recorder,
)
from novafabric.capture.record_roles import count_logical_model_calls_in_file

pytestmark = pytest.mark.timeout(60)

SPAN = "0" * 16
RUN_ID = "01HXNODRAIN0000000000000000"
CHUNKS = [f"data: chunk{i}\n\n".encode() for i in range(5)]
BODY = b"".join(CHUNKS)
#: How long a call may take to hand back the response while the server holds
#: the body. Generous: on a loaded machine a local round trip is still far
#: below it, and a hook that drains the body never returns inside it.
RETURN_BUDGET_S = 8.0
#: How long the server holds the body before giving up (unblocks a regression).
HOLD_S = 20.0


@dataclass
class _Server:
    url: str
    release: threading.Event
    stop: threading.Event
    served: list[str] = field(default_factory=list)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: Any

    def log_message(self, *args: Any) -> None:  # silence stderr
        pass

    def do_POST(self) -> None:  # noqa: N802 -- http.server's naming
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.server.state.served.append(self.path)
        if self.path == "/v1/endless":
            self._endless()
        elif self.path == "/v1/held-cl":
            self._held(chunked=False)
        else:
            self._held(chunked=True)

    def _send_part(self, data: bytes, *, chunked: bool) -> None:
        if chunked:
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        else:
            self.wfile.write(data)
        self.wfile.flush()

    def _headers(self, *, chunked: bool) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()

    def _held(self, *, chunked: bool) -> None:
        state: _Server = self.server.state
        self._headers(chunked=chunked)
        self._send_part(CHUNKS[0], chunked=chunked)
        state.release.wait(HOLD_S)  # the body stays open until the test says so
        for chunk in CHUNKS[1:]:
            self._send_part(chunk, chunked=chunked)
        if chunked:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    def _endless(self) -> None:
        state: _Server = self.server.state
        self._headers(chunked=True)
        i = 0
        try:
            while not state.stop.is_set():
                self._send_part(f"data: chunk{i}\n\n".encode(), chunked=True)
                i += 1
                state.stop.wait(0.05)
        except OSError:  # the client hung up: an SSE feed ends that way
            return


@pytest.fixture
def server() -> Iterator[_Server]:
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.daemon_threads = True
    state = _Server(
        url=f"http://127.0.0.1:{httpd.server_address[1]}",
        release=threading.Event(),
        stop=threading.Event(),
    )
    httpd.state = state  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        state.release.set()
        state.stop.set()
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Every built-in hook installed, the registry matching the local server."""
    from novafabric.capture.hooks import install_all, uninstall_all

    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "schema_version: '0.1.0'\n"
        "patterns:\n"
        "  - match: '127.0.0.1'\n"
        "    gen_ai_system: openai\n"
    )
    monkeypatch.setenv("NOVAFABRIC_URL_REGISTRY", str(registry))
    uninstall_all()
    writer = CapsuleWriter(RUN_ID, tmp_path)
    writer.open()
    recorder = EventRecorder(writer.capsule_dir, RUN_ID, "capsule-1")
    set_current_recorder(recorder)
    token = install_all(writer, SPAN)
    try:
        yield writer.capsule_dir
    finally:
        uninstall_all(token)
        clear_current_recorder(recorder)
        set_current_recorder(None)


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _returns_while_held(call: Callable[[], Any]) -> Any:
    """Run *call* in a thread; it must return while the server holds the body."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = call()
        except BaseException as exc:  # noqa: BLE001 -- re-raised below
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(RETURN_BUDGET_S)
    assert not worker.is_alive(), (
        f"the call did not return within {RETURN_BUDGET_S}s while the server held "
        "the response body: capture is draining the stream"
    )
    if "error" in box:
        raise box["error"]
    return box["value"]


def _assert_recorded(capsule: Path, *, size: int | None, library: str) -> None:
    calls = capsule / "model-calls.jsonl"
    assert count_logical_model_calls_in_file(calls) == 1  # ADR-0305
    events = [e for e in _jsonl(capsule / "network_events.jsonl") if e["library"] == library]
    assert len(events) == 1, events
    assert events[0]["status_code"] == 200
    assert events[0]["response_size_bytes"] == size


# ── requests ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("path", "size"), [("/v1/held", None), ("/v1/held-cl", len(BODY))])
def test_requests_stream_returns_before_the_body_and_is_unchanged(
    server: _Server, capture: Path, path: str, size: int | None
) -> None:
    import requests

    session = requests.Session()
    resp = _returns_while_held(
        lambda: session.post(server.url + path, json={"model": "m"}, stream=True)
    )
    parts = resp.iter_content(chunk_size=len(CHUNKS[0]))
    first = next(parts)
    server.release.set()
    rest = b"".join(parts)
    assert first + rest == BODY
    _assert_recorded(capture, size=size, library="requests")


def test_requests_endless_stream_returns(server: _Server, capture: Path) -> None:
    import requests

    session = requests.Session()
    resp = _returns_while_held(
        lambda: session.post(server.url + "/v1/endless", json={"model": "m"}, stream=True)
    )
    lines = resp.iter_lines()
    got = [next(lines) for _ in range(6)]
    assert [g for g in got if g] == [b"data: chunk0", b"data: chunk1", b"data: chunk2"]
    resp.close()
    _assert_recorded(capture, size=None, library="requests")


def test_requests_non_streamed_body_is_still_sized(server: _Server, capture: Path) -> None:
    """A body the library already read (no ``stream=True``) keeps its exact size."""
    import requests

    server.release.set()
    resp = requests.post(server.url + "/v1/held", json={"model": "m"})
    assert resp.content == BODY
    _assert_recorded(capture, size=len(BODY), library="requests")


def test_requests_error_status_is_recorded_as_an_error(capture: Path) -> None:
    """``bool(requests.Response)`` is ``response.ok``, so a 503 used to be
    recorded as status 0 and ``"success"``. It must read as the error it is."""
    import requests
    from requests.adapters import BaseAdapter

    class _Adapter(BaseAdapter):
        def send(self, request: Any, **kw: Any) -> Any:
            r = requests.Response()
            r.status_code = 503
            r._content = b"busy"  # noqa: SLF001
            r.url = request.url
            r.request = request
            return r

        def close(self) -> None:
            pass

    session = requests.Session()
    session.mount("http://127.0.0.1:9", _Adapter())
    assert session.post("http://127.0.0.1:9/v1/x", json={"model": "m"}).status_code == 503
    records = _jsonl(capture / "model-calls.jsonl")
    assert records[-1]["status"] == "error"
    events = [e for e in _jsonl(capture / "network_events.jsonl") if e["port"] == 9]
    assert events[0]["status_code"] == 503


# ── urllib3 (used directly, no requests above it) ─────────────────────────


@pytest.mark.parametrize(("path", "size"), [("/v1/held", None), ("/v1/held-cl", len(BODY))])
def test_urllib3_unpreloaded_response_returns_before_the_body(
    server: _Server, capture: Path, path: str, size: int | None
) -> None:
    import urllib3

    pool = urllib3.PoolManager()
    resp = _returns_while_held(
        lambda: pool.request("POST", server.url + path, body=b"{}", preload_content=False)
    )
    server.release.set()
    assert resp.read() == BODY
    resp.release_conn()
    _assert_recorded(capture, size=size, library="urllib3")


# ── httpx / httpx2, sync and async, send(stream=True) and client.stream() ─


@pytest.mark.parametrize("module_name", ["httpx", "httpx2"])
@pytest.mark.parametrize(("path", "size"), [("/v1/held", None), ("/v1/held-cl", len(BODY))])
def test_httpx_client_stream_returns_before_the_body(
    server: _Server, capture: Path, module_name: str, path: str, size: int | None
) -> None:
    lib = pytest.importorskip(module_name)
    with lib.Client() as client:
        cm = client.stream("POST", server.url + path, json={"model": "m"})
        resp = _returns_while_held(cm.__enter__)
        try:
            server.release.set()
            assert b"".join(resp.iter_raw()) == BODY
        finally:
            cm.__exit__(None, None, None)
    _assert_recorded(capture, size=size, library=module_name)


@pytest.mark.parametrize("module_name", ["httpx", "httpx2"])
def test_httpx_send_stream_endless_returns(
    server: _Server, capture: Path, module_name: str
) -> None:
    lib = pytest.importorskip(module_name)
    with lib.Client() as client:
        request = client.build_request("POST", server.url + "/v1/endless", json={"model": "m"})
        resp = _returns_while_held(lambda: client.send(request, stream=True))
        lines = resp.iter_lines()
        got = [line for line in (next(lines) for _ in range(6)) if line]
        assert got == ["data: chunk0", "data: chunk1", "data: chunk2"]
        resp.close()
    _assert_recorded(capture, size=None, library=module_name)


@pytest.mark.parametrize("module_name", ["httpx", "httpx2"])
def test_httpx_async_stream_returns_before_the_body(
    server: _Server, capture: Path, module_name: str
) -> None:
    lib = pytest.importorskip(module_name)

    async def main() -> bytes:
        async with lib.AsyncClient() as client:
            request = client.build_request("POST", server.url + "/v1/held", json={"model": "m"})
            resp = await asyncio.wait_for(client.send(request, stream=True), RETURN_BUDGET_S)
            server.release.set()
            body = b"".join([part async for part in resp.aiter_raw()])
            await resp.aclose()
            return body

    assert asyncio.run(main()) == BODY
    _assert_recorded(capture, size=None, library=module_name)


# ── aiohttp ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("path", "size"), [("/v1/held", None), ("/v1/held-cl", len(BODY))])
def test_aiohttp_response_returns_before_the_body(
    server: _Server, capture: Path, path: str, size: int | None
) -> None:
    aiohttp = pytest.importorskip("aiohttp")

    async def main() -> bytes:
        async with aiohttp.ClientSession() as session:
            resp = await asyncio.wait_for(
                session.post(server.url + path, json={"model": "m"}), RETURN_BUDGET_S
            )
            server.release.set()
            body = b"".join([part async for part in resp.content.iter_any()])
            resp.release()
            return body

    assert asyncio.run(main()) == BODY
    _assert_recorded(capture, size=size, library="aiohttp")


def test_aiohttp_endless_stream_returns(server: _Server, capture: Path) -> None:
    aiohttp = pytest.importorskip("aiohttp")

    async def main() -> list[bytes]:
        async with aiohttp.ClientSession() as session:
            resp = await asyncio.wait_for(
                session.post(server.url + "/v1/endless", json={"model": "m"}), RETURN_BUDGET_S
            )
            lines = [await resp.content.readline() for _ in range(6)]
            resp.close()
            return [line for line in lines if line.strip()]

    assert asyncio.run(main()) == [b"data: chunk0\n", b"data: chunk1\n", b"data: chunk2\n"]
    _assert_recorded(capture, size=None, library="aiohttp")
