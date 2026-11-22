"""Serving recorded generic-wire calls at the HTTP transport (ADR-0324, experimental).

Mocked replay answers a request to a registry model host that no dedicated server
claims (OpenAI/Anthropic SDK methods, Ollama ``/api/chat``/``/api/generate``, Bedrock
``Converse``) from the capsule's ``generic-wire`` queue, at the same seams the
dedicated transport servers use: ``httpx``/``httpx2`` ``HTTPTransport.handle_request`` /
``AsyncHTTPTransport.handle_async_request``, and urllib3
``HTTPConnectionPool.urlopen`` (which is how ``requests`` sends). One recorded call per
request, in recorded order (ADR-0300); the request must match the record (method,
registry ``gen_ai.system``, path, canonical query+body digest), or the call is the
``wire_request_mismatch`` divergence.

This module holds the pure part: the match test and the validation that turns an
untrusted record into the bytes to serve. Capsule contents are data: they are served,
never executed or evaluated, and every field is checked again here -- status range,
header allowlist, chunk count and size caps -- whatever capture wrote.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from novafabric.capture.hooks._wire_body import (
    GENERIC_WIRE_SURFACE,
    MAX_WIRE_BODY_CHARS,
    MAX_WIRE_CHUNKS,
    WIRE_REQUEST_EXT,
    WIRE_RESPONSE_EXT,
    allowed_headers,
    request_key,
)

#: The replay queue of generic-wire records (the ``api_surface`` marker value).
GENERIC_WIRE_QUEUE = GENERIC_WIRE_SURFACE


@dataclass(frozen=True)
class WireResponse:
    """What a seam serves: status, reason, allowlisted headers, body chunks."""

    status: int
    reason: str
    headers: list[tuple[str, str]]
    chunks: list[bytes]
    streamed: bool

    @property
    def body(self) -> bytes:
        return b"".join(self.chunks)

    def framed_headers(self) -> list[tuple[str, str]]:
        """The headers with the framing a client needs: a computed ``content-length``
        for a buffered body; none for a streamed one (delivered chunk by chunk)."""
        headers = list(self.headers)
        if not self.streamed:
            headers.append(("content-length", str(sum(len(c) for c in self.chunks))))
        return headers


def _ext(record: dict[str, Any]) -> dict[str, Any]:
    ext = record.get("extensions")
    return ext if isinstance(ext, dict) else {}


def unservable_reason(record: dict[str, Any]) -> str | None:
    """Why the record cannot be served back faithfully, or ``None``."""
    response = _ext(record).get(WIRE_RESPONSE_EXT)
    if not isinstance(response, dict):
        return "the record holds no wire response"
    status = response.get("status")
    if not (type(status) is int and 100 <= status <= 599):
        return "the recorded status is not an HTTP status code"
    chunks = response.get("chunks")
    if not isinstance(chunks, list) or not all(isinstance(c, str) for c in chunks):
        return "the recorded body is not a list of text chunks"
    if len(chunks) > MAX_WIRE_CHUNKS:
        return f"the recorded body has more than {MAX_WIRE_CHUNKS} chunks"
    if sum(len(c) for c in chunks) > MAX_WIRE_BODY_CHARS:
        return f"the recorded body exceeds {MAX_WIRE_BODY_CHARS} characters"
    if not isinstance(response.get("headers", []), list):
        return "the recorded headers are not a list"
    return None


def served_response(record: dict[str, Any]) -> WireResponse:
    """The response a servable record (``unservable_reason`` is ``None``) is served as.

    Headers pass the capture allowlist again, so a capsule cannot plant ``Set-Cookie``,
    ``Location`` or a framing header, whatever it holds."""
    response = _ext(record)[WIRE_RESPONSE_EXT]
    reason = response.get("reason")
    return WireResponse(
        status=int(response["status"]),
        reason=reason if isinstance(reason, str) else "",
        headers=[(k, v) for k, v in allowed_headers(response.get("headers") or [])],
        chunks=[c.encode("utf-8") for c in response["chunks"]],
        streamed=response.get("streamed") is True,
    )


def recorded_streamed(record: dict[str, Any]) -> bool:
    response = _ext(record).get(WIRE_RESPONSE_EXT)
    return isinstance(response, dict) and response.get("streamed") is True


def request_mismatch(
    record: dict[str, Any], *, method: str, url: str, body: bytes | str | None, system: str,
) -> dict[str, Any] | None:
    """How a live request differs from the record it would be served, or ``None``.

    The detail names method, ``gen_ai.system`` and path on both sides and whether the
    canonical digest differs -- never the request or recorded text."""
    recorded = _ext(record).get(WIRE_REQUEST_EXT)
    recorded = recorded if isinstance(recorded, dict) else {}
    live = request_key(method, url, body)
    recorded_system = record.get("gen_ai.system")
    differs = {
        "method": recorded.get("method") != live["method"],
        "system": recorded_system != system,
        "path": recorded.get("path") != live["path"],
        "digest": recorded.get("digest") != live["digest"],
    }
    if not any(differs.values()):
        return None
    return {
        "method": live["method"],
        "system": system,
        "path": live["path"],
        "recorded_method": recorded.get("method"),
        "recorded_system": recorded_system,
        "recorded_path": recorded.get("path"),
        "differs": sorted(k for k, v in differs.items() if v),
    }


def httpx_response(module_name: str, served: WireResponse, request: Any) -> Any:
    """The ``Response`` a patched httpx/httpx2 transport returns for a served
    generic-wire call (ADR-0324): the recorded status, reason, allowlisted headers and
    body chunks, one stream chunk per recorded chunk."""
    module = importlib.import_module(module_name)
    chunks = list(served.chunks)

    class _ServedBody(module.SyncByteStream, module.AsyncByteStream):  # type: ignore[misc, name-defined]
        def __iter__(self) -> Iterator[bytes]:
            yield from chunks

        async def __aiter__(self) -> Any:
            for chunk in chunks:
                yield chunk

    return module.Response(
        served.status, headers=served.framed_headers(), stream=_ServedBody(),
        request=request, extensions={"reason_phrase": served.reason.encode("latin-1", "replace")},
    )
