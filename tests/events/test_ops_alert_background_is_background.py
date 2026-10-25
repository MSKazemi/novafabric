"""``emit_ops_alert(background=True)`` really keeps delivery off the caller's thread
(review misc-2-10), and a webhook's retries and backoff are bounded.

The durable-record emitter ran on the caller's thread BEFORE the daemon thread started;
with ``NOVA_EVENTS_WEBHOOK`` configured, every quota-breach alert on the 429 path first
POSTed synchronously (up to 3 attempts of 5 s plus backoff) before the response left.
"""
from __future__ import annotations

import threading
from typing import Any

import pytest

from novafabric.events import alerts, sinks


class _SlowEmitter:
    def __init__(self, release: threading.Event) -> None:
        self.release = release
        self.thread: str | None = None
        self.done = threading.Event()

    def emit(self, event: Any) -> None:
        self.thread = threading.current_thread().name
        self.release.wait(5)
        self.done.set()


class _Router:
    def __init__(self) -> None:
        self.routed = threading.Event()

    def route(self, event: Any) -> None:
        self.routed.set()


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_ALERTS_WEBHOOK", "https://hooks.example/alert")


def test_the_emitter_leg_runs_on_the_background_thread(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    emitter = _SlowEmitter(release)
    router = _Router()
    monkeypatch.setattr(alerts, "build_emitter_from_env", lambda: emitter)
    monkeypatch.setattr(alerts, "_router_for", lambda cfg: router)

    alerts.emit_ops_alert(
        event_type="ops.rate_limit.sustained", severity="warning", subject_kind="ops",
        subject_ref="s", payload={}, source="test", background=True,
    )
    # We are back already although the emitter is still blocked: nothing slow ran here.
    assert not emitter.done.is_set()
    release.set()
    assert emitter.done.wait(5) and router.routed.wait(5)
    assert emitter.thread == "nova-alert-delivery"


def test_the_foreground_path_still_emits_then_routes_inline(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    release.set()
    emitter = _SlowEmitter(release)
    router = _Router()
    monkeypatch.setattr(alerts, "build_emitter_from_env", lambda: emitter)
    monkeypatch.setattr(alerts, "_router_for", lambda cfg: router)
    alerts.emit_ops_alert(
        event_type="ops.rate_limit.sustained", severity="warning", subject_kind="ops",
        subject_ref="s", payload={}, source="test", background=False,
    )
    assert emitter.done.is_set() and router.routed.is_set()
    assert emitter.thread == threading.current_thread().name


def test_retries_and_backoff_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(sinks.time, "sleep", lambda s: slept.append(s))

    def fail(*a: Any, **k: Any) -> Any:
        raise OSError("down")

    monkeypatch.setattr(sinks.httpx, "post", fail)
    result = sinks.WebhookSink("https://h.example/x", max_retries=50, backoff_s=0.5).deliver({})
    assert result.ok is False
    assert result.attempts == sinks.MAX_RETRIES_CAP + 1
    assert max(slept) <= sinks.MAX_BACKOFF_S
    assert sum(slept) <= sinks.MAX_RETRIES_CAP * sinks.MAX_BACKOFF_S
