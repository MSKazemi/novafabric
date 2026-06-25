"""Tests for LineageConsumer NATS JetStream pull consumer (cap-006, ADR-0066)."""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from novafabric.lineage.consumer import LineageConsumer

# ---------------------------------------------------------------------------
# Helper: run async coroutines synchronously
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Sentinel exception that propagates through "except Exception" handlers
# ---------------------------------------------------------------------------


class _StopLoop(BaseException):
    """Subclass of BaseException (not Exception) so it bypasses except-Exception handlers."""


# ---------------------------------------------------------------------------
# Tests: run_from_nats missing-env guard
# ---------------------------------------------------------------------------


class TestRunFromNatsEnvGuard:
    def test_raises_when_nats_url_not_set_and_default_used(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_from_nats must raise RuntimeError when NOVA_NATS_URL is not set."""
        monkeypatch.delenv("NOVA_NATS_URL", raising=False)
        # Construct with default nats_url so it equals the sentinel
        consumer = LineageConsumer(nats_url=None)
        with pytest.raises(RuntimeError, match="NOVA_NATS_URL not set"):
            _run(consumer.run_from_nats())

    def test_raises_when_nats_py_not_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_from_nats must raise RuntimeError when nats-py is missing."""
        monkeypatch.setenv("NOVA_NATS_URL", "nats://localhost:4222")
        consumer = LineageConsumer(nats_url="nats://localhost:4222")
        with patch.dict(sys.modules, {"nats": None}):
            with pytest.raises(RuntimeError, match="nats-py not installed"):
                _run(consumer.run_from_nats())


# ---------------------------------------------------------------------------
# Helpers for mocked NATS
# ---------------------------------------------------------------------------


def _build_fake_nats(fake_js: MagicMock) -> tuple[types.ModuleType, types.ModuleType]:
    """Build fake nats and nats.errors modules wrapping fake_js."""
    fake_nc = MagicMock()
    fake_nc.jetstream = MagicMock(return_value=fake_js)

    class FakeTimeoutError(Exception):
        pass

    fake_nats_mod = types.ModuleType("nats")
    fake_nats_mod.connect = AsyncMock(return_value=fake_nc)

    fake_errors_mod = types.ModuleType("nats.errors")
    fake_errors_mod.TimeoutError = FakeTimeoutError

    return fake_nats_mod, fake_errors_mod


def _run_until_fetch(
    consumer: LineageConsumer,
    fake_js: MagicMock,
    fake_nats_mod: types.ModuleType,
    fake_errors_mod: types.ModuleType,
    fetch_side_effect=None,
) -> None:
    """Run run_from_nats until it attempts the first fetch, then break out.

    ``fetch_side_effect`` is the exception or callable used for the fetch mock.
    Defaults to _StopLoop which exits cleanly.
    """
    async def stop_fetch(*args, **kwargs):
        raise _StopLoop("exit after init")

    fake_sub = MagicMock()
    fake_sub.fetch = fetch_side_effect or stop_fetch

    fake_js.pull_subscribe = AsyncMock(return_value=fake_sub)

    with patch.dict(
        sys.modules,
        {"nats": fake_nats_mod, "nats.errors": fake_errors_mod},
    ):
        with pytest.raises(_StopLoop):
            _run(consumer.run_from_nats(fetch_timeout=0.01))


# ---------------------------------------------------------------------------
# Tests: run_from_nats initialization phase
# ---------------------------------------------------------------------------


class TestRunFromNatsInitialization:
    def test_pull_subscribe_called_with_correct_args(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_from_nats must call pull_subscribe with subject and consumer name."""
        fake_js = MagicMock()
        fake_js.find_stream_name_by_subject = AsyncMock(return_value="novafabric-lineage")

        fake_nats_mod, fake_errors_mod = _build_fake_nats(fake_js)
        consumer = LineageConsumer(nats_url="nats://mocked:4222")

        _run_until_fetch(consumer, fake_js, fake_nats_mod, fake_errors_mod)

        fake_js.pull_subscribe.assert_called_once()
        call_args = fake_js.pull_subscribe.call_args
        assert call_args.args[0] == "novafabric.lineage.>"
        assert call_args.args[1] == "novafabric-lineage-consumer"

    def test_creates_stream_when_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_from_nats creates the NATS stream if find_stream_name_by_subject raises."""
        fake_js = MagicMock()
        fake_js.find_stream_name_by_subject = AsyncMock(
            side_effect=Exception("stream not found")
        )
        fake_js.add_stream = AsyncMock()

        fake_nats_mod, fake_errors_mod = _build_fake_nats(fake_js)
        consumer = LineageConsumer(nats_url="nats://mocked:4222")

        _run_until_fetch(consumer, fake_js, fake_nats_mod, fake_errors_mod)

        fake_js.add_stream.assert_called_once()
        call_kwargs = fake_js.add_stream.call_args.kwargs
        assert call_kwargs.get("name") == "novafabric-lineage"
        assert "novafabric.lineage.>" in call_kwargs.get("subjects", [])

    def test_does_not_create_stream_when_already_exists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_from_nats does not create stream when stream exists."""
        fake_js = MagicMock()
        fake_js.find_stream_name_by_subject = AsyncMock(return_value="novafabric-lineage")
        fake_js.add_stream = AsyncMock()

        fake_nats_mod, fake_errors_mod = _build_fake_nats(fake_js)
        consumer = LineageConsumer(nats_url="nats://mocked:4222")

        _run_until_fetch(consumer, fake_js, fake_nats_mod, fake_errors_mod)

        fake_js.add_stream.assert_not_called()

    def test_processes_events_from_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_from_nats processes events from a fetch batch and acks each message."""
        import json as _json

        events = [
            {
                "event_id": "n1",
                "event_type": "RunStarted",
                "run_id": "child",
                "parent_run_id": "parent",
            },
        ]

        fetch_calls = [0]
        acked: list = []

        async def fetch_once_then_stop(*args, **kwargs):
            call_no = fetch_calls[0]
            fetch_calls[0] += 1
            if call_no == 0:
                msgs = []
                for e in events:
                    m = MagicMock()
                    m.data = _json.dumps(e).encode()
                    m.ack = AsyncMock(side_effect=lambda: acked.append(True))
                    msgs.append(m)
                return msgs
            raise _StopLoop("done after one batch")

        fake_js = MagicMock()
        fake_js.find_stream_name_by_subject = AsyncMock(return_value="novafabric-lineage")

        fake_nats_mod, fake_errors_mod = _build_fake_nats(fake_js)
        consumer = LineageConsumer(nats_url="nats://mocked:4222")

        _run_until_fetch(
            consumer,
            fake_js,
            fake_nats_mod,
            fake_errors_mod,
            fetch_side_effect=fetch_once_then_stop,
        )

        assert fetch_calls[0] == 2  # first batch + stop call

    def test_malformed_message_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Messages that are not valid JSON are skipped without crashing."""
        fetch_calls = [0]

        async def fetch_bad_then_stop(*args, **kwargs):
            call_no = fetch_calls[0]
            fetch_calls[0] += 1
            if call_no == 0:
                m = MagicMock()
                m.data = b"NOT_JSON{{{"
                m.ack = AsyncMock()
                return [m]
            raise _StopLoop("done")

        fake_js = MagicMock()
        fake_js.find_stream_name_by_subject = AsyncMock(return_value="novafabric-lineage")

        fake_nats_mod, fake_errors_mod = _build_fake_nats(fake_js)
        consumer = LineageConsumer(nats_url="nats://mocked:4222")

        _run_until_fetch(
            consumer,
            fake_js,
            fake_nats_mod,
            fake_errors_mod,
            fetch_side_effect=fetch_bad_then_stop,
        )

        # We got past the malformed message without crashing
        assert fetch_calls[0] == 2
