"""Event sinks: local log + optional webhook, fanned out (ADR-0137 D3/D4).

Mirrors the sink model proven in ``promote/bypass_notify.py``: a no-op
``NullSink`` default, an append-only ``FileSink``, a best-effort
``WebhookSink`` with **bounded** retries, and a ``MultiSink`` that fans out
and swallows per-sink failures. No sink failure may ever propagate into the
workload path — the emitter additionally wraps every ``send``.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from novafabric.events.signing import SIGNATURE_HEADER

logger = logging.getLogger(__name__)

DEFAULT_MAX_RETRIES = 2
DEFAULT_TIMEOUT_S = 5.0
DEFAULT_BACKOFF_S = 0.5


class EventSink(Protocol):
    def send(self, record: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of one webhook delivery (used by the ADR-0192 alert audit trail).

    ``attempts`` counts every HTTP POST made, including bounded retries.
    """

    ok: bool
    attempts: int
    error: str | None = None


class NullSink:
    """No-op sink — active when nothing is configured (the opt-out)."""

    def send(self, record: dict[str, Any]) -> None:
        pass


class FileSink:
    """Appends each event as one JSON line to a local ``events.jsonl``.

    Works fully offline; the durable source of truth (ADR-0137 D3).
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def send(self, record: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line)


class WebhookSink:
    """POSTs each event as JSON to one user-configured URL.

    Best-effort and fail-safe: delivery retries a **bounded** number of times
    (``max_retries``, default 2) with backoff, then gives up. All exceptions
    are swallowed and logged — a broken webhook must never break a capture,
    validation, or promotion. There is no queue, redelivery daemon, or
    delivery guarantee (the local log holds the durable record).
    """

    def __init__(
        self,
        url: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_s: float = DEFAULT_BACKOFF_S,
    ) -> None:
        self._url = url
        self._timeout = timeout_s
        self._max_retries = max(0, max_retries)
        self._backoff = max(0.0, backoff_s)

    def send(self, record: dict[str, Any]) -> None:
        self.deliver(record)

    def deliver(self, record: dict[str, Any]) -> DeliveryResult:
        """Same bounded-retry POST loop as :meth:`send`, reporting the outcome.

        Used by the ADR-0192 alert router so every delivery attempt can be
        audited; still fail-safe — no exception ever propagates.
        """
        body = json.dumps(record, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        signature = record.get("signature")
        if isinstance(signature, dict) and signature.get("value"):
            headers[SIGNATURE_HEADER] = str(signature["value"])

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = httpx.post(
                    self._url, content=body, headers=headers, timeout=self._timeout
                )
                response.raise_for_status()
                return DeliveryResult(ok=True, attempts=attempt + 1)
            except Exception as exc:  # noqa: BLE001 — fail-safe, never blocks
                last_error = exc
                if attempt < self._max_retries and self._backoff > 0:
                    time.sleep(self._backoff * (2**attempt))
        logger.warning(
            "lifecycle webhook delivery failed url=%r after %d attempt(s): %s",
            self._url,
            self._max_retries + 1,
            last_error,
        )
        return DeliveryResult(
            ok=False, attempts=self._max_retries + 1, error=str(last_error)
        )


class MultiSink:
    """Fans out to all configured sinks; swallows per-sink exceptions."""

    def __init__(self, sinks: list[EventSink]) -> None:
        self._sinks = sinks

    def send(self, record: dict[str, Any]) -> None:
        for sink in self._sinks:
            try:
                sink.send(record)
            except Exception as exc:  # noqa: BLE001 — fail-safe, never blocks
                logger.warning(
                    "lifecycle event sink %s raised: %s", type(sink).__name__, exc
                )
