"""Error taxonomy mapping (ADR-0202 D5; spec §Error taxonomy).

Every listed status maps to its subclass; envelope codes pass through
verbatim; non-envelope bodies produce ``unknown_error``; transport failures
raise ``NovaFabricTransportError`` (never ``NovaFabricAPIError``).
"""

from __future__ import annotations

import httpx
import pytest

from novafabric.client import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    NovaFabricAPIError,
    NovaFabricClient,
    NovaFabricClientError,
    NovaFabricTimeout,
    NovaFabricTransportError,
    PreconditionFailedError,
    RateLimitedError,
    RetryConfig,
    ServerError,
    ValidationFailedError,
)

from .conftest import ScriptedTransport


def _envelope(code: str, message: str = "boom", details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def _client(*script: httpx.Response | Exception) -> NovaFabricClient:
    return NovaFabricClient(
        "http://x.example/v0",
        transport=ScriptedTransport(*script),
        retries=RetryConfig(max_attempts=1),
    )


@pytest.mark.parametrize(
    ("status", "code", "expected_cls"),
    [
        (401, "unauthenticated", AuthenticationError),
        (403, "forbidden", AuthorizationError),
        (404, "not_found", NotFoundError),
        (409, "conflict", ConflictError),
        (409, "parent_not_found", ConflictError),
        (409, "idempotency_conflict", ConflictError),
        (412, "precondition_failed", PreconditionFailedError),
        (422, "validation_error", ValidationFailedError),
        (422, "supersedes_not_found", ValidationFailedError),
        (429, "rate_limited", RateLimitedError),
        (500, "internal", ServerError),
        (503, "unavailable", ServerError),
    ],
)
def test_status_maps_to_subclass_and_code_passes_verbatim(
    status: int, code: str, expected_cls: type[NovaFabricAPIError]
) -> None:
    client = _client(httpx.Response(status, json=_envelope(code)))
    with pytest.raises(expected_cls) as exc_info:
        client.get_capsule("r1")
    err = exc_info.value
    assert err.status == status
    assert err.code == code
    assert err.message == "boom"
    assert err.meta.status == status
    assert isinstance(err, NovaFabricClientError)


def test_unlisted_4xx_is_bare_api_error() -> None:
    client = _client(httpx.Response(400, json=_envelope("bad_request")))
    with pytest.raises(NovaFabricAPIError) as exc_info:
        client.get_capsule("r1")
    assert type(exc_info.value) is NovaFabricAPIError
    assert exc_info.value.code == "bad_request"


def test_details_are_surfaced() -> None:
    client = _client(
        httpx.Response(409, json=_envelope("conflict", details={"run_id": "r1"}))
    )
    with pytest.raises(ConflictError) as exc_info:
        client.get_capsule("r1")
    assert exc_info.value.details == {"run_id": "r1"}


class TestMalformedEnvelope:
    def test_html_body_falls_back_to_unknown_error(self) -> None:
        client = _client(
            httpx.Response(502, text="<html>Bad Gateway</html>")
        )
        with pytest.raises(ServerError) as exc_info:
            client.get_capsule("r1")
        assert exc_info.value.code == "unknown_error"
        assert exc_info.value.message == "HTTP 502 Bad Gateway"

    def test_empty_body_falls_back_to_unknown_error(self) -> None:
        client = _client(httpx.Response(500))
        with pytest.raises(ServerError) as exc_info:
            client.get_capsule("r1")
        assert exc_info.value.code == "unknown_error"
        assert exc_info.value.message == "HTTP 500 Internal Server Error"

    def test_json_but_not_envelope_falls_back(self) -> None:
        # FastAPI request-validation failures look like {"detail": [...]}.
        client = _client(httpx.Response(422, json={"detail": [{"loc": ["body"]}]}))
        with pytest.raises(ValidationFailedError) as exc_info:
            client.get_capsule("r1")
        assert exc_info.value.code == "unknown_error"

    def test_envelope_with_non_string_code_falls_back(self) -> None:
        client = _client(httpx.Response(404, json={"error": {"code": 5, "message": "x"}}))
        with pytest.raises(NotFoundError) as exc_info:
            client.get_capsule("r1")
        assert exc_info.value.code == "unknown_error"

    def test_non_dict_details_dropped(self) -> None:
        body = {"error": {"code": "conflict", "message": "m", "details": ["x"]}}
        client = _client(httpx.Response(409, json=body))
        with pytest.raises(ConflictError) as exc_info:
            client.get_capsule("r1")
        assert exc_info.value.details is None


class TestRateLimited:
    def test_retry_after_seconds_parsed(self) -> None:
        client = _client(
            httpx.Response(
                429, json=_envelope("rate_limited"), headers={"Retry-After": "7"}
            )
        )
        with pytest.raises(RateLimitedError) as exc_info:
            client.get_capsule("r1")
        assert exc_info.value.retry_after == 7.0

    def test_missing_retry_after_is_none(self) -> None:
        client = _client(httpx.Response(429, json=_envelope("rate_limited")))
        with pytest.raises(RateLimitedError) as exc_info:
            client.get_capsule("r1")
        assert exc_info.value.retry_after is None


class TestTransportErrors:
    def test_connect_error_raises_transport_error(self) -> None:
        client = _client(httpx.ConnectError("connection refused"))
        with pytest.raises(NovaFabricTransportError) as exc_info:
            client.get_capsule("r1")
        assert not isinstance(exc_info.value, NovaFabricAPIError)
        assert isinstance(exc_info.value.cause, httpx.ConnectError)

    def test_read_timeout_raises_timeout(self) -> None:
        client = _client(httpx.ReadTimeout("read timed out"))
        with pytest.raises(NovaFabricTimeout) as exc_info:
            client.get_capsule("r1")
        assert isinstance(exc_info.value, NovaFabricTransportError)
        assert isinstance(exc_info.value.cause, httpx.ReadTimeout)

    def test_read_timeout_is_not_retried(self, no_sleep: list[float]) -> None:
        transport = ScriptedTransport(httpx.ReadTimeout("slow"))
        client = NovaFabricClient(
            "http://x.example/v0", transport=transport, retries=RetryConfig()
        )
        with pytest.raises(NovaFabricTimeout):
            client.get_capsule("r1")
        assert len(transport.requests) == 1
