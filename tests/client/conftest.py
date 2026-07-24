"""Shared fixtures/helpers for the ``novafabric.client`` test suite (ADR-0202 P1).

Two transports, no real network anywhere:

- :class:`ScriptedTransport` — a sync ``httpx.BaseTransport`` that replays a
  script of responses/exceptions and records every request (error paths,
  retries, header assertions).
- :class:`SyncASGITransport` — a sync bridge over ``httpx.ASGITransport`` so
  the sync client can talk to the real in-process FastAPI server app.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Iterator, Union

import httpx
import pytest

from novafabric.client import reset_deprecation_warnings

ScriptItem = Union[httpx.Response, Exception, Callable[[httpx.Request], httpx.Response]]


@pytest.fixture(autouse=True)
def _fresh_deprecation_registry() -> Iterator[None]:
    """The warn-once registry is process-wide; keep tests independent."""
    reset_deprecation_warnings()
    yield
    reset_deprecation_warnings()


class ScriptedTransport(httpx.BaseTransport):
    """Replays *script* items in order; the last item repeats forever.

    Items may be ``httpx.Response`` instances, exceptions (raised), or
    callables ``(request) -> Response``. Every request is recorded.
    """

    def __init__(self, *script: ScriptItem) -> None:
        if not script:
            raise ValueError("ScriptedTransport needs at least one item")
        self._script = list(script)
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.read()  # materialize the body so tests can inspect it
        self.requests.append(request)
        item = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(request)
        return item


class SyncASGITransport(httpx.BaseTransport):
    """Sync facade over ``httpx.ASGITransport`` for the sync client.

    ``httpx.ASGITransport`` is async-only; the P1 client is sync. Each request
    is bridged through a private event loop — hermetic, no sockets.
    """

    def __init__(self, app: Any) -> None:
        self._transport = httpx.ASGITransport(app=app)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        content = request.read()

        async def _run() -> httpx.Response:
            bridged = httpx.Request(
                request.method, request.url, headers=request.headers, content=content
            )
            response = await self._transport.handle_async_request(bridged)
            try:
                body = await response.aread()
            finally:
                await response.aclose()
            return httpx.Response(
                response.status_code, headers=response.headers, content=body
            )

        return asyncio.run(_run())


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the retry sleep with a recorder; returns the recorded delays."""
    delays: list[float] = []
    monkeypatch.setattr("novafabric.client._client._sleep", delays.append)
    return delays
