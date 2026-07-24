"""Webhook delivery dispatcher (ADR-0205 D4/D5, experimental).

One in-process dispatcher per server app: a **bounded** ``queue.Queue`` fed by
non-blocking :meth:`WebhookDispatcher.enqueue_event` calls from server-side
event sources, drained by a single daemon worker thread that matches events
against enabled subscriptions, writes delivery rows, signs the exact body
bytes (Stripe-style ``t=...,v1=...``), and POSTs via the existing ADR-0137
:class:`~novafabric.events.sinks.WebhookSink` (one POST per scheduled attempt;
the sink's in-call retry loop is disabled so the log's attempt count stays
meaningful).

Hard invariants, inherited from ADR-0137 D4:

- ``enqueue_event`` never blocks and never raises into the request path; on a
  full queue the event is dropped **with** a hash-chained
  ``webhook.queue.overflow`` audit entry (at most one per bounded window) and
  a counter — never silently, never blocking.
- Scheduled retries live in dispatcher memory (P1 restart semantics): rows
  left ``pending``/``retrying`` at process exit stay visible in the delivery
  log and are manually redeliverable, but are not auto-resumed on startup.

The 5-attempt backoff schedule (``0s/30s/2m/10m/1h``) is spec-normative in
shape; only the total attempt count is configurable (1-10). The schedule is
injectable for tests.
"""

from __future__ import annotations

import heapq
import itertools
import json
import logging
import queue
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from novafabric.events.hygiene import sanitize_record
from novafabric.events.model import LifecycleEvent
from novafabric.events.sinks import DeliveryResult, WebhookSink
from novafabric.server import webhooks as store
from novafabric.trust.novaseal.signing_backend import KeyWrappingBackend

logger = logging.getLogger(__name__)

#: Spec-normative backoff offsets from enqueue (seconds): 0s/30s/2m/10m/1h.
DEFAULT_SCHEDULE_S: tuple[float, ...] = (0.0, 30.0, 120.0, 600.0, 3600.0)

#: At most one overflow audit entry per this window (seconds).
OVERFLOW_AUDIT_WINDOW_S = 60.0

_HEADER_SIGNATURE = "X-NovaFabric-Signature"
_HEADER_HOOK_ID = "X-NovaFabric-Webhook-Id"
_HEADER_DELIVERY_ID = "X-NovaFabric-Delivery-Id"
_HEADER_EVENT_ID = "X-NovaFabric-Event-Id"
_HEADER_EVENT_TYPE = "X-NovaFabric-Event-Type"


@dataclass(frozen=True)
class DispatchConfig:
    """Resolved dispatcher configuration (``server.webhooks.*``)."""

    queue_max: int = 1000
    max_attempts: int = 5
    timeout_s: float = 5.0
    retention_days: int = store.DEFAULT_RETENTION_DAYS
    retention_rows: int = store.DEFAULT_RETENTION_ROWS
    # Injectable for tests; the geometric shape is otherwise fixed (spec).
    schedule_s: tuple[float, ...] = DEFAULT_SCHEDULE_S
    overflow_audit_window_s: float = OVERFLOW_AUDIT_WINDOW_S
    # ADR-0192 dedup discipline for ops.* events (bounded map).
    dedup_window_s: float = 300.0
    dedup_max_entries: int = 10_000

    def delay_for(self, chain_attempt: int) -> float:
        """Offset (from chain start) of *chain_attempt* (0-based)."""
        if chain_attempt < len(self.schedule_s):
            return self.schedule_s[chain_attempt]
        return self.schedule_s[-1]


@dataclass
class _EventTask:
    event_record: dict[str, Any]
    workspace: str | None = None


@dataclass(order=True)
class _Attempt:
    due: float
    seq: int
    delivery_id: str = field(compare=False)
    hook_id: str = field(compare=False)
    event_id: str = field(compare=False)
    event_type: str = field(compare=False)
    body: bytes = field(compare=False)
    chain_attempt: int = field(compare=False)  # 0-based within this chain
    chain_start: float = field(compare=False)


_STOP = object()


class WebhookDispatcher:
    """Bounded-queue, single-worker webhook delivery engine (ADR-0205 D4)."""

    def __init__(
        self,
        *,
        db_path: Path | None,
        config: DispatchConfig | None = None,
        audit_log_path: Path | None = None,
        wrapping_backend: KeyWrappingBackend | None = None,
    ) -> None:
        self._db_path = db_path
        self._config = config or DispatchConfig()
        self._audit_log_path = audit_log_path
        self._backend = wrapping_backend
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=self._config.queue_max)
        self._attempts: list[_Attempt] = []  # heap, guarded by _lock
        self._lock = threading.Lock()
        self._seq = itertools.count()
        self._thread: threading.Thread | None = None
        self._dropped = 0
        self._last_overflow_audit = float("-inf")
        # (event_type, subject_ref) -> monotonic time of last delivery match.
        self._dedup: OrderedDict[tuple[str, str], float] = OrderedDict()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="nova-webhook-dispatch"
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:  # drain path still sees _STOP eventually
            with self._queue.mutex:
                self._queue.queue.appendleft(_STOP)
                self._queue.not_empty.notify()
        self._thread.join(timeout=timeout)
        self._thread = None

    @property
    def dropped_count(self) -> int:
        """Events dropped on queue overflow since start (bounded-loss counter)."""
        return self._dropped

    def pending_attempts(self) -> int:
        with self._lock:
            return len(self._attempts)

    # -- request-path API (never blocks, never raises) ---------------------

    def enqueue_event(
        self, event: LifecycleEvent, *, workspace: str | None = None
    ) -> None:
        """Non-blocking enqueue from server-side event sources (ADR-0137 D4).

        *workspace* is the event's workspace attribution (None = unattributed:
        delivered only to unscoped webhooks — ADR-0205 D2 honesty bound).
        """
        try:
            record = sanitize_record(event.to_record())
            task = _EventTask(event_record=record, workspace=workspace)
            try:
                self._queue.put_nowait(task)
            except queue.Full:
                self._on_overflow(event.type.value, event.event_id)
        except Exception as exc:  # noqa: BLE001 — must never reach the request path
            logger.warning("webhook enqueue failed: %s", exc)

    def ping(self, hook_id: str, *, requested_by: str) -> str:
        """Send a synthetic ``webhook.ping`` through the full delivery path.

        Targets exactly one subscription (bypassing the event-type filter so a
        filtered hook is still testable); returns the delivery row id.
        """
        from novafabric.events.model import EventType, Subject, SubjectKind

        store.get_webhook(hook_id, db_path=self._db_path)  # 404 before enqueue
        event = LifecycleEvent(
            type=EventType.WEBHOOK_PING,
            subject=Subject(kind=SubjectKind.OPS, ref=hook_id),
            payload={"hook_id": hook_id, "requested_by": requested_by},
        )
        record = sanitize_record(event.to_record())
        body = json.dumps(record, ensure_ascii=False).encode("utf-8")
        delivery_id = store.insert_delivery(
            hook_id,
            event_id=event.event_id,
            event_type=event.type.value,
            payload=body.decode("utf-8"),
            status="pending",
            next_attempt_at=self._iso_now(),
            retention_days=self._config.retention_days,
            retention_rows=self._config.retention_rows,
            db_path=self._db_path,
        )
        self._schedule_chain(
            delivery_id=delivery_id,
            hook_id=hook_id,
            event_id=event.event_id,
            event_type=event.type.value,
            body=body,
        )
        return delivery_id

    def redeliver(
        self, hook_id: str, delivery_id: str, *, actor: str = "webhook-dispatcher"
    ) -> None:
        """Re-enqueue a stored terminal-failed delivery as a fresh attempt chain.

        Allowed only on terminal ``failed`` (or ``dropped``-with-payload) rows
        (spec); the stored payload is re-posted on the same row and
        ``redelivery_of`` records the manual redelivery.
        """
        row = store.get_delivery(delivery_id, db_path=self._db_path)
        if row["hook_id"] != hook_id:
            raise store.UnknownDeliveryError(
                f"delivery '{delivery_id}' does not belong to webhook '{hook_id}'"
            )
        if row["status"] not in ("failed", "dropped") or not row["payload"]:
            raise store.NotRedeliverableError(
                f"delivery '{delivery_id}' is {row['status']!r}; only terminal "
                f"'failed' (or 'dropped' with a stored payload) rows can be "
                f"redelivered"
            )
        store.mark_delivery(
            delivery_id,
            status="pending",
            next_attempt_at=self._iso_now(),
            redelivery_of=delivery_id,
            db_path=self._db_path,
        )
        self._audit(
            "webhook.redeliver",
            actor=actor,
            resource_id=delivery_id,
            details={"hook_id": hook_id, "event_id": row["event_id"]},
        )
        self._schedule_chain(
            delivery_id=delivery_id,
            hook_id=hook_id,
            event_id=row["event_id"],
            event_type=row["event_type"],
            body=row["payload"].encode("utf-8"),
        )

    # -- worker ------------------------------------------------------------

    def _run(self) -> None:
        while True:
            self._process_due_attempts()
            timeout = self._time_to_next_due()
            try:
                task = self._queue.get(timeout=timeout)
            except queue.Empty:
                continue
            if task is _STOP:
                return
            try:
                self._process_event(task)
            except Exception as exc:  # noqa: BLE001 — the worker must survive
                logger.warning("webhook event processing failed: %s", exc)

    def _time_to_next_due(self) -> float:
        with self._lock:
            if not self._attempts:
                return 0.2
            return min(max(self._attempts[0].due - time.monotonic(), 0.0), 0.2)

    def _process_due_attempts(self) -> None:
        while True:
            with self._lock:
                if not self._attempts or self._attempts[0].due > time.monotonic():
                    return
                attempt = heapq.heappop(self._attempts)
            try:
                self._attempt(attempt)
            except Exception as exc:  # noqa: BLE001 — the worker must survive
                logger.warning(
                    "webhook delivery attempt failed internally "
                    "(delivery=%s): %s",
                    attempt.delivery_id,
                    exc,
                )

    def _process_event(self, task: _EventTask) -> None:
        record = task.event_record
        event_type = str(record.get("type", ""))
        event_id = str(record.get("event_id", ""))
        subject_ref = str((record.get("subject") or {}).get("ref", ""))
        if event_type.startswith("ops.") and self._suppressed(
            event_type, subject_ref
        ):
            return
        body = json.dumps(record, ensure_ascii=False).encode("utf-8")
        for hook in store.list_webhooks(db_path=self._db_path):
            if not self._matches(hook, event_type, task.workspace):
                continue
            delivery_id = store.insert_delivery(
                hook["hook_id"],
                event_id=event_id,
                event_type=event_type,
                payload=body.decode("utf-8"),
                status="pending",
                next_attempt_at=self._iso_now(),
                retention_days=self._config.retention_days,
                retention_rows=self._config.retention_rows,
                db_path=self._db_path,
            )
            self._schedule_chain(
                delivery_id=delivery_id,
                hook_id=hook["hook_id"],
                event_id=event_id,
                event_type=event_type,
                body=body,
            )

    @staticmethod
    def _matches(
        hook: dict[str, Any], event_type: str, workspace: str | None
    ) -> bool:
        if bool(hook["disabled"]):
            return False
        if hook["event_types"] is not None and event_type not in hook["event_types"]:
            return False
        if hook["workspace"] is not None:
            # Scoped webhooks receive only events attributed to their
            # workspace; unattributed events go to unscoped webhooks only.
            return bool(workspace == hook["workspace"])
        return True

    def _suppressed(self, event_type: str, subject_ref: str) -> bool:
        """ADR-0192 dedup discipline: one delivery per (type, subject, window)."""
        key = (event_type, subject_ref)
        now = time.monotonic()
        last = self._dedup.get(key)
        if last is not None and (now - last) < self._config.dedup_window_s:
            self._dedup.move_to_end(key)
            return True
        self._dedup[key] = now
        self._dedup.move_to_end(key)
        while len(self._dedup) > self._config.dedup_max_entries:
            self._dedup.popitem(last=False)
        return False

    # -- one HTTP POST per scheduled attempt -------------------------------

    def _schedule_chain(
        self,
        *,
        delivery_id: str,
        hook_id: str,
        event_id: str,
        event_type: str,
        body: bytes,
    ) -> None:
        now = time.monotonic()
        attempt = _Attempt(
            due=now + self._config.delay_for(0),
            seq=next(self._seq),
            delivery_id=delivery_id,
            hook_id=hook_id,
            event_id=event_id,
            event_type=event_type,
            body=body,
            chain_attempt=0,
            chain_start=now,
        )
        with self._lock:
            heapq.heappush(self._attempts, attempt)

    def _attempt(self, attempt: _Attempt) -> None:
        try:
            hook = store.get_webhook(attempt.hook_id, db_path=self._db_path)
            secret = store.load_secret(
                attempt.hook_id,
                db_path=self._db_path,
                wrapping_backend=self._backend,
            )
        except (store.UnknownWebhookError, store.SecretUnavailableError) as exc:
            store.record_attempt(
                attempt.delivery_id,
                status="failed",
                status_code=None,
                error=str(exc),
                next_attempt_at=None,
                db_path=self._db_path,
            )
            return

        timestamp = int(time.time())
        headers = {
            _HEADER_SIGNATURE: store.sign_delivery(secret, attempt.body, timestamp),
            _HEADER_HOOK_ID: attempt.hook_id,
            _HEADER_DELIVERY_ID: attempt.delivery_id,
            _HEADER_EVENT_ID: attempt.event_id,
            _HEADER_EVENT_TYPE: attempt.event_type,
        }
        # One POST per scheduled attempt: the sink's in-call retry loop is
        # disabled (max_retries=0) so the persisted attempt count is exact.
        sink = WebhookSink(
            hook["url"], timeout_s=self._config.timeout_s, max_retries=0
        )
        result = sink.deliver({}, headers=headers, body=attempt.body)
        self._record_result(attempt, result)

    def _record_result(self, attempt: _Attempt, result: DeliveryResult) -> None:
        chain_attempt = attempt.chain_attempt + 1  # 1-based count so far
        if result.ok:
            store.record_attempt(
                attempt.delivery_id,
                status="delivered",
                status_code=result.status_code,
                error=None,
                next_attempt_at=None,
                db_path=self._db_path,
            )
        elif chain_attempt >= self._config.max_attempts:
            store.record_attempt(
                attempt.delivery_id,
                status="failed",
                status_code=result.status_code,
                error=result.error,
                next_attempt_at=None,
                db_path=self._db_path,
            )
        else:
            due = attempt.chain_start + self._config.delay_for(chain_attempt)
            next_at = self._iso_in(max(due - time.monotonic(), 0.0))
            store.record_attempt(
                attempt.delivery_id,
                status="retrying",
                status_code=result.status_code,
                error=result.error,
                next_attempt_at=next_at,
                db_path=self._db_path,
            )
            retry = _Attempt(
                due=due,
                seq=next(self._seq),
                delivery_id=attempt.delivery_id,
                hook_id=attempt.hook_id,
                event_id=attempt.event_id,
                event_type=attempt.event_type,
                body=attempt.body,
                chain_attempt=chain_attempt,
                chain_start=attempt.chain_start,
            )
            with self._lock:
                heapq.heappush(self._attempts, retry)
        self._audit_attempt(attempt, result, chain_attempt)

    # -- overflow + audit --------------------------------------------------

    def _on_overflow(self, event_type: str, event_id: str) -> None:
        """Drop with audit: one ``webhook.queue.overflow`` entry per window."""
        self._dropped += 1
        now = time.monotonic()
        if now - self._last_overflow_audit < self._config.overflow_audit_window_s:
            return
        self._last_overflow_audit = now
        self._audit(
            "webhook.queue.overflow",
            actor="webhook-dispatcher",
            resource_id=event_id,
            details={
                "event_type": event_type,
                "queue_max": self._config.queue_max,
                "dropped_total": self._dropped,
            },
        )

    def _audit_attempt(
        self, attempt: _Attempt, result: DeliveryResult, chain_attempt: int
    ) -> None:
        self._audit(
            "webhook.delivery",
            actor="webhook-dispatcher",
            resource_id=attempt.event_id,
            details={
                "hook_id": attempt.hook_id,
                "delivery_id": attempt.delivery_id,
                "event_type": attempt.event_type,
                "outcome": "delivered" if result.ok else "error",
                "attempt": chain_attempt,
                **({"error": result.error} if result.error is not None else {}),
            },
        )

    def _audit(
        self,
        event_type_value: str,
        *,
        actor: str,
        resource_id: str,
        details: dict[str, Any],
    ) -> None:
        """Hash-chained audit append; failures logged, never raised (D4)."""
        try:
            from novafabric.audit import AuditEventType, AuditLog, _paths

            path = self._audit_log_path or _paths.AUDIT_LOG_PATH
            AuditLog(path).append(
                event_type=AuditEventType(event_type_value),
                actor=actor,
                resource_id=resource_id,
                details=details,
            )
        except Exception as exc:  # noqa: BLE001 — audit must never break dispatch
            logger.warning("webhook audit append failed: %s", exc)

    # -- small helpers -----------------------------------------------------

    @staticmethod
    def _iso_now() -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _iso_in(seconds: float) -> str:
        from datetime import datetime, timedelta, timezone

        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
