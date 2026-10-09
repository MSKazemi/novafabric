"""Rebuild a recorded SDK exception so mocked replay raises it again (issue #16).

When the captured workload's SDK call raised -- a rate limit, a 400, a 5xx after
the SDK's own retries, a timeout -- the SDK hook wrote one *logical* error record
(``status: error``, an ``error`` block, ``io.novafabric.api_surface``) and, since
the call went through the wire hook, one *transport* record per HTTP attempt
(ADR-0305). Mocked replay serves the logical record at its recorded position by
raising, inside the replayed process, the **same SDK exception class** the
workload saw, built from ``extensions["io.novafabric.sdk_error"]``
(``capture/hooks/_sdk_errors.py``). Transport records are never served.

Exception classes come from an explicit allow-list per SDK
(:data:`ALLOWED_SDK_ERRORS`) and are looked up as attributes of the SDK's own
top-level package -- never imported by a module or class name read from the
capsule. A record this module cannot rebuild faithfully (an unknown class, a
missing status, a body that was not recorded, a capsule captured before the
error detail was recorded) raises :class:`UnreconstructableError`; the
dispatcher turns that into a named divergence (fail closed).
"""

from __future__ import annotations

import importlib
import json
import sys
from types import ModuleType
from typing import Any

from novafabric.capture.hooks._sdk_errors import SDK_ERROR_EXT
from novafabric.capture.hooks._sdk_streams import RESPONSE_STATUS_EXT, STREAM_ERROR_EVENT_EXT

#: How an allow-listed exception class is constructed.
#: ``status``: ``cls(message, response=<http response>, body=body)`` (an HTTP
#: 4xx/5xx the SDK turned into an exception); ``connection``:
#: ``cls(message=message, request=<http request>)``; ``timeout``:
#: ``cls(request=<http request>)``; ``api_error``: ``cls(message, request,
#: body=body)`` (``openai.APIError``, which ``openai._streaming`` raises when a
#: stream that already started carries an ``error`` payload). These are the
#: constructors the ``openai`` and ``anthropic`` SDKs (both Stainless-generated)
#: define.
STATUS = "status"
CONNECTION = "connection"
TIMEOUT = "timeout"
API_ERROR = "api_error"

_STATUS_CLASSES = (
    "BadRequestError",
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "ConflictError",
    "UnprocessableEntityError",
    "RateLimitError",
    "InternalServerError",
    "APIStatusError",
)

#: Per SDK (``gen_ai.system``), the exception classes mocked replay re-raises.
#: Anything else -- ``OAuthError``, ``APIResponseValidationError``, a
#: ``TypeError`` from bad arguments, another library's exception -- is refused.
ALLOWED_SDK_ERRORS: dict[str, dict[str, str]] = {
    "openai": {
        **dict.fromkeys(_STATUS_CLASSES, STATUS),
        "APIConnectionError": CONNECTION,
        "APITimeoutError": TIMEOUT,
        "APIError": API_ERROR,
    },
    "anthropic": {
        **dict.fromkeys(_STATUS_CLASSES, STATUS),
        "RequestTooLargeError": STATUS,
        "ServiceUnavailableError": STATUS,
        "OverloadedError": STATUS,
        "DeadlineExceededError": STATUS,
        "APIConnectionError": CONNECTION,
        "APITimeoutError": TIMEOUT,
    },
}

#: HTTP client modules replay may import to build the synthetic request/response.
_HTTP_MODULES = ("httpx", "httpx2")

_PLACEHOLDER_URL = "http://replay.invalid/"


class UnreconstructableError(Exception):
    """A recorded model error that mocked replay cannot raise faithfully."""


def is_recorded_model_error(record: dict[str, Any]) -> bool:
    """A record whose call failed: a non-success status with an ``error`` block.

    Callers apply the transport / queue rules first (``_contract``); this test
    alone never makes a record servable.
    """
    return record.get("status", "success") != "success" and isinstance(
        record.get("error"), dict
    )


def recorded_error_type(record: dict[str, Any]) -> str:
    error = record.get("error")
    return str(error.get("type") or "") if isinstance(error, dict) else ""


def _ext(record: dict[str, Any], key: str) -> Any:
    ext = record.get("extensions")
    return ext.get(key) if isinstance(ext, dict) else None


def is_returned_failed_response(record: dict[str, Any]) -> bool:
    """A failed call the SDK *returned* rather than raised (issue #16).

    A Responses API ``Response`` with ``status: failed``: ``openai`` parses it
    like any other body (``cast_to=Response``) and returns it, and streams its
    ``response.failed`` event like any other. Served as a response, never
    raised -- but only when capture recorded its status verbatim
    (``io.novafabric.response_status``); an older record is refused.

    Also a Responses stream that delivered an ``error`` event: ``openai._streaming``
    *yields* that event (it raises only for a payload with a top-level ``error``
    key). Served as the delivered events followed by that event -- only when
    capture kept it verbatim (``io.novafabric.stream_error_event`` with its
    message); one omitted for size is refused.
    """
    if not is_recorded_model_error(record) or isinstance(_ext(record, SDK_ERROR_EXT), dict):
        return False
    status = _ext(record, RESPONSE_STATUS_EXT)
    if isinstance(status, dict) and status.get("status") == "failed":
        return True
    event = _ext(record, STREAM_ERROR_EVENT_EXT)
    return (
        isinstance(event, dict)
        and isinstance(event.get("message"), str)
        and isinstance(record.get("nova.streaming"), dict)
    )


def raised_mid_stream(record: dict[str, Any]) -> bool:
    """A recorded SDK exception raised while a stream was being iterated.

    The SDK call returned a stream (``nova.streaming`` is recorded only then),
    delivered ``chunk_count`` chunks, and then raised; an exception raised at
    ``create`` has no ``nova.streaming`` block.
    """
    return (
        is_recorded_model_error(record)
        and isinstance(_ext(record, SDK_ERROR_EXT), dict)
        and isinstance(record.get("nova.streaming"), dict)
    )


def _detail(record: dict[str, Any]) -> dict[str, Any]:
    detail = _ext(record, SDK_ERROR_EXT)
    if not isinstance(detail, dict):
        if isinstance(_ext(record, STREAM_ERROR_EVENT_EXT), dict):
            raise UnreconstructableError(
                "the stream delivered an error event that capture could not keep "
                f"verbatim (extensions[{STREAM_ERROR_EVENT_EXT!r}] has no message), "
                "so replay cannot serve it as delivered"
            )
        if record.get("gen_ai.response.id") or record.get("gen_ai.response.choices"):
            raise UnreconstructableError(
                "the call returned a response with status 'failed' (a Responses API "
                "response, which the SDK returns rather than raises), and this capsule "
                "was captured before that status and its error were recorded "
                f"(no extensions[{RESPONSE_STATUS_EXT!r}]); re-capture to replay it"
            )
        raise UnreconstructableError(
            "the capsule was captured before SDK error details were recorded "
            f"(no extensions[{SDK_ERROR_EXT!r}]); re-capture to replay recorded errors"
        )
    return detail


def _http_module(recorded: Any, exc_class: type) -> ModuleType:
    """The HTTP client module to build the synthetic request/response with.

    The module the recorded ``exc.response`` came from when it is one of
    :data:`_HTTP_MODULES`; else the one the SDK's exceptions module uses; else
    ``httpx``.
    """
    if isinstance(recorded, str) and recorded in _HTTP_MODULES:
        try:
            return importlib.import_module(recorded)
        except ImportError:
            pass
    sdk_module = sys.modules.get(exc_class.__module__)
    for name in _HTTP_MODULES[::-1]:
        candidate = getattr(sdk_module, name, None)
        if isinstance(candidate, ModuleType) and hasattr(candidate, "Response"):
            return candidate
    return importlib.import_module("httpx")


def _content(detail: dict[str, Any]) -> bytes:
    text = detail.get("response_text")
    if isinstance(text, str):
        return text.encode()
    body = detail.get("body")
    if body is None:
        return b""
    if isinstance(body, str):
        return body.encode()
    return json.dumps(body).encode()


def rebuild_sdk_error(record: dict[str, Any]) -> BaseException:
    """The exception the recorded call raised, rebuilt with the SDK's own class.

    Raises :class:`UnreconstructableError` with the reason when that cannot be
    done faithfully. Never imports a name taken from the record: the SDK is one
    of :data:`ALLOWED_SDK_ERRORS`' keys and the class one of its values.
    """
    detail = _detail(record)
    sdk = record.get("gen_ai.system")
    allowed = ALLOWED_SDK_ERRORS.get(str(sdk))
    if allowed is None or detail.get("sdk") != sdk:
        raise UnreconstructableError(
            f"the error was recorded for SDK {detail.get('sdk')!r} on a "
            f"{sdk!r} call; only {sorted(ALLOWED_SDK_ERRORS)} errors are replayed"
        )
    class_name = detail.get("class")
    kind = allowed.get(str(class_name))
    if kind is None:
        raise UnreconstructableError(
            f"{sdk}.{class_name} is not an exception class mocked replay rebuilds "
            f"(allowed: {', '.join(sorted(allowed))})"
        )
    message = detail.get("message")
    if not isinstance(message, str):
        raise UnreconstructableError("no error message was recorded")
    try:
        sdk_module = importlib.import_module(str(sdk))
    except ImportError as exc:
        raise UnreconstructableError(
            f"the {sdk} SDK is not importable in the replayed process"
        ) from exc
    exc_class = getattr(sdk_module, str(class_name), None)
    if not (isinstance(exc_class, type) and issubclass(exc_class, Exception)):
        raise UnreconstructableError(
            f"the installed {sdk} SDK has no exception class {class_name!r}"
        )

    status = detail.get("status_code")
    if kind == STATUS and (
        not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599
    ):
        raise UnreconstructableError(f"no HTTP status was recorded for {class_name}")
    if kind in (STATUS, API_ERROR) and "body_omitted" in detail:
        raise UnreconstructableError(
            f"the error body was not recorded ({detail['body_omitted']})"
        )

    http = _http_module(detail.get("response_type"), exc_class)
    # The constructor signatures are the SDK's (see STATUS/CONNECTION/TIMEOUT).
    factory: Any = exc_class
    try:
        request = http.Request(
            str(detail.get("request_method") or "POST"),
            str(detail.get("request_url") or record.get("endpoint") or _PLACEHOLDER_URL),
        )
        if kind == TIMEOUT:
            built = factory(request=request)
        elif kind == CONNECTION:
            built = factory(message=message, request=request)
        elif kind == API_ERROR:
            built = factory(message, request, body=detail.get("body"))
        else:
            headers = detail.get("response_headers")
            response = http.Response(
                int(status),  # type: ignore[arg-type]
                headers=headers if isinstance(headers, dict) else {},
                content=_content(detail),
                request=request,
            )
            built = factory(message, response=response, body=detail.get("body"))
    except Exception as exc:  # noqa: BLE001 -- a different SDK/HTTP layout
        raise UnreconstructableError(
            f"{sdk}.{class_name} could not be constructed: {type(exc).__name__}: {exc}"
        ) from exc
    if str(built) != message:
        raise UnreconstructableError(
            f"the rebuilt {class_name} reads {str(built)!r}, not the recorded message"
        )
    if kind == STATUS and getattr(built, "status_code", status) != status:
        raise UnreconstructableError(
            f"the rebuilt {class_name} reports status {getattr(built, 'status_code', None)}, "
            f"not the recorded {status}"
        )
    assert isinstance(built, BaseException)
    return built
