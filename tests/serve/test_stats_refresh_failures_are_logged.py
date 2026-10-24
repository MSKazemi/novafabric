"""The stats refresh loop reports its failures (review serve-11).

``_stats_refresh_loop`` caught every exception with ``pass``: a broken registry DB or
watcher left the SSE stream and the runs index silently stale, with nothing in the
log. A failure is logged when it starts and when it changes, and recovery is logged;
a persistent identical failure does not repeat every 30 s.
"""
from __future__ import annotations

import logging

import pytest

from novafabric.serve.app import _RefreshFailureLog


def test_a_failure_is_logged_once_until_it_changes_and_recovery_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    log = _RefreshFailureLog("stats refresh")
    with caplog.at_level(logging.INFO, logger="novafabric.serve.app"):
        for _ in range(3):
            log.failed(OSError("database is locked"))
        log.failed(ValueError("bad row"))
        log.succeeded()
        log.succeeded()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(warnings) == 2  # one per distinct failure, not one per tick
    assert "database is locked" in warnings[0] and "bad row" in warnings[1]
    assert len(infos) == 1 and "recovered" in infos[0]


def test_no_failure_means_no_log(caplog: pytest.LogCaptureFixture) -> None:
    log = _RefreshFailureLog("stats refresh")
    with caplog.at_level(logging.INFO, logger="novafabric.serve.app"):
        log.succeeded()
    assert caplog.records == []
