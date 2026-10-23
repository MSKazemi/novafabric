"""Bounded request-body reads for ``nova serve`` ingest routes.

``await request.body()`` buffers whatever the client sends before any size check
can run. :func:`read_body_capped` reads the ASGI stream chunk by chunk and stops
as soon as the running total passes the cap, so the memory a single request can
pin is bounded by the cap (plus one chunk) whether or not the client sent an
honest ``Content-Length``. A declared ``Content-Length`` over the cap is refused
before the first chunk is read.
"""

from __future__ import annotations

from typing import Protocol

from fastapi import HTTPException

__all__ = ["BodyTooLargeError", "read_body_capped"]


class _StreamingRequest(Protocol):
    @property
    def headers(self) -> "object": ...

    def stream(self) -> "object": ...


class BodyTooLargeError(HTTPException):
    """413: the request body exceeds the route's cap."""

    def __init__(self, limit: int) -> None:
        super().__init__(
            status_code=413, detail=f"request body exceeds {limit} bytes"
        )


async def read_body_capped(request: _StreamingRequest, limit: int) -> bytes:
    """Return the request body, or raise :class:`BodyTooLargeError` past *limit* bytes."""
    declared = request.headers.get("content-length")  # type: ignore[attr-defined]
    if declared is not None:
        try:
            if int(declared) > limit:
                raise BodyTooLargeError(limit)
        except ValueError:
            pass  # malformed header: the streamed count below still bounds it
    buf = bytearray()
    async for chunk in request.stream():  # type: ignore[attr-defined]
        buf.extend(chunk)
        if len(buf) > limit:
            raise BodyTooLargeError(limit)
    return bytes(buf)
