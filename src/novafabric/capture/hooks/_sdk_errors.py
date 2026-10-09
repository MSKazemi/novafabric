"""What an SDK exception carried, recorded so mocked replay can raise it again.

The SDK hooks used to record a failed call as ``error: {type, message}`` only.
That names the exception but not what a workload's ``except`` block reads: the
HTTP status, the parsed error body, the request id, the ``retry-after`` header.
:func:`describe_sdk_error` captures those additively under
``extensions["io.novafabric.sdk_error"]``; ``replay/_model_errors.py`` rebuilds
the exception from it, through an explicit allow-list of SDK exception classes.

Never raises: capture must not fail the workload because an exception object has
an unexpected shape.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

#: Reverse-DNS extension key on an SDK error record.
SDK_ERROR_EXT = "io.novafabric.sdk_error"

#: Largest error body (serialized JSON, characters) recorded verbatim. A larger
#: body is not recorded (``body_omitted``), and replay then refuses to rebuild
#: the error rather than raise it with a body the workload never saw.
MAX_ERROR_BODY_CHARS = 64 * 1024

#: Response headers kept: what retry and rate-limit handling reads. Never
#: authorization material -- these are response headers, and only these names.
_HEADER_NAMES = frozenset({
    "content-type",
    "retry-after",
    "retry-after-ms",
    "x-request-id",
    "request-id",
    "x-should-retry",
})
_HEADER_PREFIXES = ("x-ratelimit-", "anthropic-ratelimit-")
_MAX_HEADERS = 32
_MAX_HEADER_VALUE = 256

#: HTTP client modules whose ``Response`` type is recorded by name (and the
#: only ones replay will import to rebuild one).
_HTTP_MODULES = ("httpx", "httpx2")


def _strip_query(url: str) -> str:
    """The URL without query string or fragment (where a key could hide)."""
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        return ""


def _kept_headers(response: Any) -> dict[str, str]:
    headers: dict[str, str] = {}
    try:
        items = list(response.headers.items())
    except Exception:  # noqa: BLE001 -- no headers / unexpected type
        return headers
    for name, value in items:
        key = str(name).lower()
        if key in _HEADER_NAMES or key.startswith(_HEADER_PREFIXES):
            headers[key] = str(value)[:_MAX_HEADER_VALUE]
            if len(headers) >= _MAX_HEADERS:
                break
    return headers


def mark_failed_by(record: dict[str, Any], exc: BaseException, sdk: str) -> None:
    """Mark a model-call record as failed by ``exc``, as an SDK error record is.

    Used for a stream that raised while it was iterated (issue #16): the record
    keeps the content delivered before the exception and ``nova.streaming``,
    and gains ``status: error``, the ``error`` block and ``SDK_ERROR_EXT``.
    """
    try:
        message = str(exc)
    except Exception:  # noqa: BLE001 -- capture must never fail the workload
        message = ""
    record["status"] = "error"
    record["error"] = {"type": type(exc).__name__, "message": message, "traceback_ref": None}
    record.setdefault("extensions", {})[SDK_ERROR_EXT] = describe_sdk_error(exc, sdk)


def describe_sdk_error(exc: BaseException, sdk: str) -> dict[str, Any]:
    """The replay-relevant detail of an exception an SDK call raised.

    Keys (all but ``sdk``/``class``/``message`` optional): ``sdk``, ``class``
    (the exception's class name), ``module`` (its defining module, for
    information only -- replay never imports it), ``message`` (the exception's
    ``message`` attribute, else ``str(exc)``), ``status_code``, ``body`` (the
    SDK's parsed error body, verbatim, or ``body_omitted`` with a reason),
    ``request_id``, ``request_method``, ``request_url`` (query stripped),
    ``response_headers`` (rate-limit and retry headers only),
    ``response_text`` (the raw error body the SDK parsed ``body`` from) and
    ``response_type`` (the HTTP client module of ``exc.response``).
    """
    info: dict[str, Any] = {"sdk": sdk, "class": type(exc).__name__}
    try:
        info["module"] = str(type(exc).__module__)
        message = getattr(exc, "message", None)
        info["message"] = message if isinstance(message, str) else str(exc)
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and not isinstance(status, bool):
            info["status_code"] = status
        if hasattr(exc, "body"):
            body = exc.body
            try:
                encoded = json.dumps(body)
            except (TypeError, ValueError):
                info["body_omitted"] = "not JSON-serializable"
            else:
                if len(encoded) > MAX_ERROR_BODY_CHARS:
                    info["body_omitted"] = f"larger than {MAX_ERROR_BODY_CHARS} characters"
                else:
                    info["body"] = body
        request_id = getattr(exc, "request_id", None)
        if isinstance(request_id, str) and request_id:
            info["request_id"] = request_id
        try:
            request = getattr(exc, "request", None)
        except Exception:  # noqa: BLE001 -- httpx raises when a request is unset
            request = None
        if request is not None:
            method = getattr(request, "method", None)
            if isinstance(method, str) and method:
                info["request_method"] = method
            url = _strip_query(str(getattr(request, "url", "") or ""))
            if url:
                info["request_url"] = url
        response = getattr(exc, "response", None)
        if response is not None:
            info["response_headers"] = _kept_headers(response)
            try:
                text = response.text
            except Exception:  # noqa: BLE001 -- e.g. a streamed body never read
                text = None
            if isinstance(text, str) and len(text) <= MAX_ERROR_BODY_CHARS:
                info["response_text"] = text
            module = str(type(response).__module__).split(".", 1)[0]
            if module in _HTTP_MODULES:
                info["response_type"] = module
    except Exception:  # noqa: BLE001 -- capture must never fail the workload
        pass
    return info
