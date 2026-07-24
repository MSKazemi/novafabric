"""Typed error taxonomy for the NovaFabric Python client (ADR-0202 D5).

Everything the client raises derives from :class:`NovaFabricClientError`:

- :class:`NovaFabricConfigError` — bad/missing local configuration; no request
  was made.
- :class:`NovaFabricTransportError` — the server never answered (wraps
  ``httpx.TransportError``; ``.cause`` is set). :class:`NovaFabricTimeout` is
  the timeout specialization.
- :class:`NovaFabricAPIError` — the server answered with a non-2xx response.
  Subclasses are chosen by HTTP status; the envelope ``code`` passes through
  verbatim and is never enumerated exhaustively, so new server codes cannot
  break the client. Non-envelope bodies fall back to ``code="unknown_error"``
  (byte-compatible with the TS SDK fallback).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

    from novafabric.client._models import ResponseMeta


class NovaFabricClientError(Exception):
    """Base class for every error raised by ``novafabric.client``."""


class NovaFabricConfigError(NovaFabricClientError):
    """Bad or missing local configuration. No request was made."""


class NovaFabricTransportError(NovaFabricClientError):
    """The server never answered (connection failure, protocol error).

    ``cause`` carries the underlying ``httpx.TransportError``.
    """

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.cause = cause


class NovaFabricTimeout(NovaFabricTransportError):
    """A request timed out (wraps ``httpx.TimeoutException``)."""


class NovaFabricAPIError(NovaFabricClientError):
    """Any non-2xx HTTP response, decoded from the ADR-0017 error envelope."""

    def __init__(
        self,
        *,
        status: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        meta: ResponseMeta,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.meta = meta


class AuthenticationError(NovaFabricAPIError):
    """401 — missing, malformed, revoked, or expired credential."""


class AuthorizationError(NovaFabricAPIError):
    """403 — authenticated, but the role does not permit the operation."""


class NotFoundError(NovaFabricAPIError):
    """404 — the resource does not exist."""


class ConflictError(NovaFabricAPIError):
    """409 — duplicate resource, missing parent, or idempotency conflict."""


class PreconditionFailedError(NovaFabricAPIError):
    """412 — a server-side gate blocked the operation."""


class ValidationFailedError(NovaFabricAPIError):
    """422 — the request body failed server-side validation."""


class RateLimitedError(NovaFabricAPIError):
    """429 — rate limited; ``retry_after`` is the parsed header, when present."""

    def __init__(
        self,
        *,
        status: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        meta: ResponseMeta,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(
            status=status, code=code, message=message, details=details, meta=meta
        )
        self.retry_after = retry_after


class ServerError(NovaFabricAPIError):
    """5xx — the server failed."""


_STATUS_CLASSES: dict[int, type[NovaFabricAPIError]] = {
    401: AuthenticationError,
    403: AuthorizationError,
    404: NotFoundError,
    409: ConflictError,
    412: PreconditionFailedError,
    422: ValidationFailedError,
    429: RateLimitedError,
}


def _decode_envelope(response: httpx.Response) -> tuple[str, str, dict[str, Any] | None]:
    """Return ``(code, message, details)`` from the error envelope.

    Envelope decoding never raises: any parse failure falls back to
    ``("unknown_error", "HTTP <status> <reason>", None)`` — the TS SDK contract.
    """
    try:
        body = response.json()
        error = body["error"]
        code = error["code"]
        message = error["message"]
        if not isinstance(code, str) or not isinstance(message, str):
            raise TypeError("non-string code/message")
        details = error.get("details")
        if details is not None and not isinstance(details, dict):
            details = None
        return code, message, details
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        reason = response.reason_phrase
        message = f"HTTP {response.status_code} {reason}".rstrip()
        return "unknown_error", message, None


def error_from_response(
    response: httpx.Response, meta: ResponseMeta
) -> NovaFabricAPIError:
    """Map a non-2xx ``httpx.Response`` to the typed error taxonomy.

    Subclass by status (unlisted 4xx ⇒ bare :class:`NovaFabricAPIError`,
    5xx ⇒ :class:`ServerError`); envelope codes pass through verbatim.
    """
    status = response.status_code
    code, message, details = _decode_envelope(response)
    if status == 429:
        from novafabric.client._retry import parse_retry_after

        return RateLimitedError(
            status=status,
            code=code,
            message=message,
            details=details,
            meta=meta,
            retry_after=parse_retry_after(response.headers.get("Retry-After")),
        )
    cls = _STATUS_CLASSES.get(status)
    if cls is None:
        cls = ServerError if status >= 500 else NovaFabricAPIError
    return cls(status=status, code=code, message=message, details=details, meta=meta)
