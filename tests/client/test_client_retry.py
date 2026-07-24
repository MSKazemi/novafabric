"""Bounded GET-only retries (ADR-0202 D7; spec §Timeouts and retries).

GET-only, bounded by ``max_attempts``, honors ``Retry-After``, exhaustion
re-raises the final failure — verified with a counting scripted transport.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from novafabric.client import (
    NovaFabricClient,
    NovaFabricTransportError,
    RateLimitedError,
    RetryConfig,
    ServerError,
)
from novafabric.client._retry import compute_backoff, parse_retry_after

from .conftest import ScriptedTransport


def _envelope(code: str) -> dict:
    return {"error": {"code": code, "message": "boom", "details": {}}}


def _ok() -> httpx.Response:
    return httpx.Response(200, json={"items": [], "next_cursor": None, "total": 0})


def _client(transport: ScriptedTransport, **retry_kwargs: object) -> NovaFabricClient:
    return NovaFabricClient(
        "http://x.example/v0",
        transport=transport,
        retries=RetryConfig(**retry_kwargs),  # type: ignore[arg-type]
    )


class TestGetRetries:
    def test_retries_on_503_then_succeeds(self, no_sleep: list[float]) -> None:
        transport = ScriptedTransport(
            httpx.Response(503, json=_envelope("unavailable")), _ok()
        )
        client = _client(transport)
        result = client.list_capsules()
        assert result.meta.status == 200
        assert len(transport.requests) == 2
        assert len(no_sleep) == 1

    def test_retries_on_429_honoring_retry_after(self, no_sleep: list[float]) -> None:
        transport = ScriptedTransport(
            httpx.Response(
                429, json=_envelope("rate_limited"), headers={"Retry-After": "3"}
            ),
            _ok(),
        )
        client = _client(transport)
        assert client.list_capsules().meta.status == 200
        assert no_sleep == [3.0]

    def test_budget_exhaustion_reraises_final_rate_limited(
        self, no_sleep: list[float]
    ) -> None:
        transport = ScriptedTransport(
            httpx.Response(429, json=_envelope("rate_limited"))
        )
        client = _client(transport, max_attempts=3)
        with pytest.raises(RateLimitedError):
            client.list_capsules()
        assert len(transport.requests) == 3
        assert len(no_sleep) == 2  # no sleep after the final attempt

    def test_budget_exhaustion_reraises_final_server_error(
        self, no_sleep: list[float]
    ) -> None:
        transport = ScriptedTransport(httpx.Response(502, text="bad gateway"))
        client = _client(transport, max_attempts=2)
        with pytest.raises(ServerError) as exc_info:
            client.list_capsules()
        assert exc_info.value.status == 502
        assert len(transport.requests) == 2

    def test_max_attempts_one_disables_retrying(self, no_sleep: list[float]) -> None:
        transport = ScriptedTransport(httpx.Response(503, json=_envelope("x")))
        client = _client(transport, max_attempts=1)
        with pytest.raises(ServerError):
            client.list_capsules()
        assert len(transport.requests) == 1
        assert no_sleep == []

    def test_connect_error_retried_then_succeeds(self, no_sleep: list[float]) -> None:
        transport = ScriptedTransport(httpx.ConnectError("refused"), _ok())
        client = _client(transport)
        assert client.list_capsules().meta.status == 200
        assert len(transport.requests) == 2

    def test_connect_error_exhaustion_raises_transport_error(
        self, no_sleep: list[float]
    ) -> None:
        transport = ScriptedTransport(httpx.ConnectError("refused"))
        client = _client(transport, max_attempts=3)
        with pytest.raises(NovaFabricTransportError):
            client.list_capsules()
        assert len(transport.requests) == 3

    def test_non_retry_status_is_not_retried(self, no_sleep: list[float]) -> None:
        transport = ScriptedTransport(httpx.Response(500, json=_envelope("internal")))
        client = _client(transport)
        with pytest.raises(ServerError):
            client.list_capsules()
        assert len(transport.requests) == 1  # 500 is not in retry_statuses


class TestPostNeverRetried:
    def test_post_score_not_retried_on_503(self, no_sleep: list[float]) -> None:
        transport = ScriptedTransport(httpx.Response(503, json=_envelope("x")))
        client = _client(transport)
        with pytest.raises(ServerError):
            client.submit_score("r1", {"name": "n"})
        assert len(transport.requests) == 1
        assert no_sleep == []

    def test_post_upload_not_retried_on_connect_error(
        self, no_sleep: list[float]
    ) -> None:
        transport = ScriptedTransport(httpx.ConnectError("refused"))
        client = _client(transport)
        with pytest.raises(NovaFabricTransportError):
            client.upload_capsule(b"zipbytes")
        assert len(transport.requests) == 1
        assert no_sleep == []


class TestBackoffMath:
    def test_compute_backoff_no_jitter_is_capped_exponential(self) -> None:
        config = RetryConfig(jitter="none")
        assert compute_backoff(config, 0) == 0.5
        assert compute_backoff(config, 1) == 1.0
        assert compute_backoff(config, 2) == 2.0
        assert compute_backoff(config, 10) == 8.0  # backoff_max cap

    def test_compute_backoff_full_jitter_within_bounds(self) -> None:
        config = RetryConfig()
        for retry_index in range(6):
            ceiling = min(8.0, 0.5 * 2.0**retry_index)
            for _ in range(20):
                delay = compute_backoff(config, retry_index)
                assert 0.0 <= delay <= ceiling


class TestParseRetryAfter:
    def test_seconds(self) -> None:
        assert parse_retry_after("5") == 5.0

    def test_capped_at_30(self) -> None:
        assert parse_retry_after("120") == 30.0

    def test_negative_clamped_to_zero(self) -> None:
        assert parse_retry_after("-3") == 0.0

    def test_http_date(self) -> None:
        when = datetime.now(timezone.utc) + timedelta(seconds=10)
        parsed = parse_retry_after(format_datetime(when, usegmt=True))
        assert parsed is not None
        assert 0.0 <= parsed <= 30.0

    def test_http_date_in_past_is_zero(self) -> None:
        when = datetime.now(timezone.utc) - timedelta(seconds=60)
        assert parse_retry_after(format_datetime(when, usegmt=True)) == 0.0

    def test_absent_or_garbage_is_none(self) -> None:
        assert parse_retry_after(None) is None
        assert parse_retry_after("") is None
        assert parse_retry_after("soon") is None
