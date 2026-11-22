"""Serving model calls whichever ``httpx`` transport a client sends through
(ADR-0324 amendment, experimental).

Mocked replay answers Ollama and generic-wire calls at ``HTTPTransport.handle_request``
/ ``AsyncHTTPTransport.handle_async_request`` (``httpx`` and ``httpx2``). Capture
records one level up, at ``Client.send``, whatever transport the client holds. A
client built with another transport -- ``httpx.MockTransport``, a ``BaseTransport``
subclass that delegates to ``HTTPTransport`` (proxy, retry and caching libraries), an
``HTTPTransport`` subclass that overrides ``handle_request`` -- was therefore recorded
with its response but never reached the serving seam: the call ran on the workload's
transport and replay never saw it.

The client asks ``Client._transport_for_url(url)`` for the transport of every request
(``_send_single_request``; ``httpx`` 0.28, ``httpx2`` 2.x). Replay wraps what that
returns when the transport is not one the seam already covers, in a
:class:`ServingTransport` that consults the same serving code first and hands an
unclaimed request to the workload's transport unchanged. Everything above the transport
-- auth flows, redirects, event hooks, cookies, ``elapsed`` -- runs exactly as for a
served ``HTTPTransport`` call.

A request is handled once: when the wrapper passes a request on (not claimed, or a
tolerated divergence that runs it live and has already been counted), it does so inside
:func:`handled_upstream`, and the seam below -- the ``HTTPTransport`` a delegating
transport calls, or a nested client -- passes it through untouched. An SDK call that
replay refuses and lets run live (``--permissive``) is passed on the same way: it is
counted at the SDK method, never a second time on the wire (ADR-0305).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol

#: The client method mocked replay patches so every transport is served:
#: (module, class, async).
SERVED_CLIENT_SURFACES: tuple[tuple[str, str, bool], ...] = (
    ("httpx", "Client", False),
    ("httpx", "AsyncClient", True),
    ("httpx2", "Client", False),
    ("httpx2", "AsyncClient", True),
)

#: The ``Client`` / ``AsyncClient`` method that picks a request's transport.
TRANSPORT_FOR_URL = "_transport_for_url"

_handled: ContextVar[bool] = ContextVar("novafabric_replay_handled_upstream", default=False)


@contextmanager
def handled_upstream() -> Iterator[None]:
    """Mark the request(s) sent inside the block as already served-or-counted by
    replay: no transport seam below claims them again."""
    token = _handled.set(True)
    try:
        yield
    finally:
        _handled.reset(token)


def is_handled_upstream() -> bool:
    return _handled.get()


class Answerer(Protocol):
    """The serving side (``MockModelDispatcher``)."""

    def transport_target(self, request: Any) -> str | None:
        """The replay queue that claims this request, or ``None`` (pass through)."""

    def transport_answer(
        self, module_name: str, target: str, request: Any, content: bytes,
        *, asynchronous: bool,
    ) -> Any | None:
        """The served ``Response``, or ``None`` when a tolerated divergence lets the
        request run live (already counted). Raises under strict."""


class ServingTransport:
    """A sync transport that serves claimed requests before the workload's own."""

    __slots__ = ("_answerer", "_inner", "_module_name")

    def __init__(self, module_name: str, inner: Any, answerer: Answerer) -> None:
        self._module_name = module_name
        self._inner = inner
        self._answerer = answerer

    def handle_request(self, request: Any) -> Any:
        target = self._answerer.transport_target(request)
        if target is not None:
            response = self._answerer.transport_answer(
                self._module_name, target, request, request.read(), asynchronous=False
            )
            if response is not None:
                return response
        with handled_upstream():
            return self._inner.handle_request(request)


class AsyncServingTransport:
    """The async counterpart of :class:`ServingTransport`."""

    __slots__ = ("_answerer", "_inner", "_module_name")

    def __init__(self, module_name: str, inner: Any, answerer: Answerer) -> None:
        self._module_name = module_name
        self._inner = inner
        self._answerer = answerer

    async def handle_async_request(self, request: Any) -> Any:
        target = self._answerer.transport_target(request)
        if target is not None:
            response = self._answerer.transport_answer(
                self._module_name, target, request, await request.aread(), asynchronous=True
            )
            if response is not None:
                return response
        with handled_upstream():
            return await self._inner.handle_async_request(request)


def seam_attr(asynchronous: bool) -> str:
    return "handle_async_request" if asynchronous else "handle_request"


def covered(transport: Any, seam_functions: tuple[Any, ...], *, asynchronous: bool) -> bool:
    """The transport's request method is a patched serving seam itself (a plain
    ``HTTPTransport`` or a subclass that does not override it), or it is already a
    serving wrapper: it needs no wrapper."""
    if isinstance(transport, (ServingTransport, AsyncServingTransport)):
        return True
    method = getattr(type(transport), seam_attr(asynchronous), None)
    return any(method is seam for seam in seam_functions)


def serving_transport(
    module_name: str, transport: Any, answerer: Answerer, *, asynchronous: bool
) -> Any:
    if asynchronous:
        return AsyncServingTransport(module_name, transport, answerer)
    return ServingTransport(module_name, transport, answerer)
