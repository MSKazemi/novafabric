"""Operational alert routing layered on the ADR-0137 emitter (ADR-0192, experimental).

One outbound mechanism: `ops.*` events ride the existing emitter/sinks — this
module adds only the alert-specific bounding (per-rule dedup window over a
bounded map, per-endpoint severity routing, per-endpoint event-type allowlist)
and the hash-chained delivery audit trail. There is **no second dispatcher**.

Strictly opt-in: with no ``NOVA_ALERTS_*`` configuration the router is a
byte-identical no-op — nothing is emitted (not even to a configured
``NOVA_EVENTS_LOG``) and nothing is sent (ADR-0192 D3). There is no default
destination and no hardcoded URL.

Environment variables (following the ``NOVA_EVENTS_*`` conventions)
-------------------------------------------------------------------
``NOVA_ALERTS_WEBHOOK``
    One or more alert endpoint URLs, comma-separated. User-configured only —
    the allowlist; only listed endpoints ever receive a delivery.
``NOVA_ALERTS_MIN_SEVERITY``
    Minimum severity per endpoint (``info|warning|critical``). One value
    applies to all endpoints; a comma-separated list maps positionally to
    the URLs. Default ``warning``.
``NOVA_ALERTS_TYPES``
    Comma-separated allowlist of ``ops.*`` event types every endpoint
    accepts. Default: all six. Non-``ops.*`` names are ignored with a warning.
``NOVA_ALERTS_DEDUP_WINDOW_S``
    Dedup window in seconds — at most one delivery per (event type, subject,
    window). Default 300.
``NOVA_ALERTS_DEDUP_MAX_ENTRIES``
    Bound on the in-process dedup map (oldest-entry eviction above it — the
    ADR-0179 bucket-map discipline). Default 10000.
``NOVA_ALERTS_MAX_RETRIES`` / ``NOVA_ALERTS_TIMEOUT_S``
    Bounded webhook retry count (default 2) and per-request timeout
    (default 5.0), exactly as for ``NOVA_EVENTS_*``.
``NOVA_ALERTS_SIGN_SECRET`` / ``NOVA_ALERTS_SIGN_KEYID``
    Optional HMAC-SHA256 signing override; falls back to
    ``NOVA_EVENTS_SIGN_SECRET`` / ``NOVA_EVENTS_SIGN_KEYID`` (ADR-0192 D4).
    Never written to config, the log, or any payload.
``NOVA_ALERTS_AUDIT_LOG``
    Path of the hash-chained delivery audit log (default: the house
    ``novafabric.audit`` log). One entry per attempted delivery (ADR-0192 D6).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from novafabric.audit import AUDIT_LOG_PATH, AuditEventType, AuditLog
from novafabric.events.emitter import (
    _float_env,
    _int_env,
    _nova_version,
    build_emitter_from_env,
)
from novafabric.events.hygiene import sanitize_record
from novafabric.events.model import (
    EventSeverity,
    EventType,
    LifecycleEvent,
    Subject,
    SubjectKind,
)
from novafabric.events.signing import sign_record
from novafabric.events.sinks import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_S,
    DeliveryResult,
    WebhookSink,
)

logger = logging.getLogger(__name__)

ENV_WEBHOOK = "NOVA_ALERTS_WEBHOOK"
ENV_MIN_SEVERITY = "NOVA_ALERTS_MIN_SEVERITY"
ENV_TYPES = "NOVA_ALERTS_TYPES"
ENV_DEDUP_WINDOW_S = "NOVA_ALERTS_DEDUP_WINDOW_S"
ENV_DEDUP_MAX_ENTRIES = "NOVA_ALERTS_DEDUP_MAX_ENTRIES"
ENV_MAX_RETRIES = "NOVA_ALERTS_MAX_RETRIES"
ENV_TIMEOUT_S = "NOVA_ALERTS_TIMEOUT_S"
ENV_SIGN_SECRET = "NOVA_ALERTS_SIGN_SECRET"
ENV_SIGN_KEYID = "NOVA_ALERTS_SIGN_KEYID"
ENV_AUDIT_LOG = "NOVA_ALERTS_AUDIT_LOG"

# The ADR-0137 signing convention the alerts config falls back to (ADR-0192 D4).
_ENV_EVENTS_SIGN_SECRET = "NOVA_EVENTS_SIGN_SECRET"
_ENV_EVENTS_SIGN_KEYID = "NOVA_EVENTS_SIGN_KEYID"

DEFAULT_DEDUP_WINDOW_S = 300.0
DEFAULT_DEDUP_MAX_ENTRIES = 10_000
DEFAULT_MIN_SEVERITY = EventSeverity.WARNING

#: The curated, closed `ops.*` family (ADR-0192 D1).
OPS_EVENT_TYPES: frozenset[str] = frozenset(
    t.value for t in EventType if t.value.startswith("ops.")
)

_SEVERITY_ORDER: dict[EventSeverity, int] = {
    EventSeverity.INFO: 0,
    EventSeverity.WARNING: 1,
    EventSeverity.CRITICAL: 2,
}


@dataclass(frozen=True)
class AlertEndpoint:
    """One allowlisted alert destination (URL, minimum severity, type filter)."""

    endpoint_id: str
    url: str
    min_severity: EventSeverity = DEFAULT_MIN_SEVERITY
    event_types: frozenset[str] = OPS_EVENT_TYPES

    def accepts(self, event_type: str, severity: EventSeverity) -> bool:
        return (
            event_type in self.event_types
            and _SEVERITY_ORDER[severity] >= _SEVERITY_ORDER[self.min_severity]
        )


@dataclass(frozen=True)
class AlertsConfig:
    """Resolved alert-router configuration (all user-supplied; no defaults fire)."""

    endpoints: tuple[AlertEndpoint, ...] = ()
    dedup_window_s: float = DEFAULT_DEDUP_WINDOW_S
    dedup_max_entries: int = DEFAULT_DEDUP_MAX_ENTRIES
    max_retries: int = DEFAULT_MAX_RETRIES
    timeout_s: float = DEFAULT_TIMEOUT_S
    sign_secret: bytes | None = None
    sign_keyid: str = "default"
    audit_log_path: Path | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.endpoints)


def _parse_severities(raw: str, count: int) -> list[EventSeverity]:
    """One value applies to all endpoints; a list maps positionally."""
    values = [v.strip().lower() for v in raw.split(",") if v.strip()]
    parsed: list[EventSeverity] = []
    for value in values:
        try:
            parsed.append(EventSeverity(value))
        except ValueError:
            logger.warning(
                "ignoring unknown %s value %r (expected info|warning|critical)",
                ENV_MIN_SEVERITY,
                value,
            )
            parsed.append(DEFAULT_MIN_SEVERITY)
    if not parsed:
        return [DEFAULT_MIN_SEVERITY] * count
    if len(parsed) == 1:
        return parsed * count
    if len(parsed) != count:
        logger.warning(
            "%s has %d value(s) for %d endpoint(s); extra values ignored, "
            "missing ones default to %s",
            ENV_MIN_SEVERITY,
            len(parsed),
            count,
            DEFAULT_MIN_SEVERITY.value,
        )
    return (parsed + [DEFAULT_MIN_SEVERITY] * count)[:count]


def _parse_types(raw: str) -> frozenset[str]:
    """Allowlist of `ops.*` types; unknown names are ignored with a warning."""
    names = [t.strip() for t in raw.split(",") if t.strip()]
    if not names:
        return OPS_EVENT_TYPES
    accepted = frozenset(n for n in names if n in OPS_EVENT_TYPES)
    for unknown in set(names) - set(accepted):
        logger.warning("ignoring non-ops %s entry %r", ENV_TYPES, unknown)
    return accepted or OPS_EVENT_TYPES


def load_alerts_config_from_env(
    env: Mapping[str, str] | None = None,
) -> AlertsConfig:
    """Build the alert-router configuration from ``NOVA_ALERTS_*`` variables."""
    env = os.environ if env is None else env
    urls = [
        u.strip() for u in env.get(ENV_WEBHOOK, "").strip().split(",") if u.strip()
    ]
    severities = _parse_severities(env.get(ENV_MIN_SEVERITY, ""), len(urls))
    event_types = _parse_types(env.get(ENV_TYPES, ""))
    endpoints = tuple(
        AlertEndpoint(
            endpoint_id=f"webhook-{i + 1}",
            url=url,
            min_severity=severities[i],
            event_types=event_types,
        )
        for i, url in enumerate(urls)
    )
    secret_raw = env.get(ENV_SIGN_SECRET, "") or env.get(_ENV_EVENTS_SIGN_SECRET, "")
    keyid = (
        env.get(ENV_SIGN_KEYID, "").strip()
        or env.get(_ENV_EVENTS_SIGN_KEYID, "").strip()
    )
    audit_raw = env.get(ENV_AUDIT_LOG, "").strip()
    return AlertsConfig(
        endpoints=endpoints,
        dedup_window_s=_float_env(env, ENV_DEDUP_WINDOW_S, DEFAULT_DEDUP_WINDOW_S),
        dedup_max_entries=_int_env(
            env, ENV_DEDUP_MAX_ENTRIES, DEFAULT_DEDUP_MAX_ENTRIES
        ),
        max_retries=_int_env(env, ENV_MAX_RETRIES, DEFAULT_MAX_RETRIES),
        timeout_s=_float_env(env, ENV_TIMEOUT_S, DEFAULT_TIMEOUT_S),
        sign_secret=secret_raw.encode("utf-8") if secret_raw else None,
        sign_keyid=keyid or "default",
        audit_log_path=Path(audit_raw) if audit_raw else None,
    )


class AlertRouter:
    """Dedup + severity routing for `ops.*` events over the ADR-0137 sink core.

    Layered ON the emitter: hygiene (:func:`sanitize_record`), signing
    (:func:`sign_record`), and delivery (:class:`WebhookSink`) are the
    existing ADR-0137 primitives — this class only decides *whether* and
    *where* one event goes, and audits every attempted delivery.

    ``route`` is fail-safe by construction: every failure is logged and
    swallowed, never raised into the operation that raised the alert
    (ADR-0137 D4 inherited as a hard invariant).
    """

    def __init__(
        self,
        config: AlertsConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._clock = clock
        # (event_type, subject_ref) -> monotonic time of the last delivery.
        # Bounded, oldest-evicted — the ADR-0179 bucket-map discipline.
        self._dedup: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._lock = threading.Lock()
        self._sinks: dict[str, WebhookSink] = {
            ep.endpoint_id: WebhookSink(
                ep.url, timeout_s=config.timeout_s, max_retries=config.max_retries
            )
            for ep in config.endpoints
        }
        self._audit: AuditLog | None = None

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def dedup_entries(self) -> int:
        """Current dedup-map size (bounded by ``dedup_max_entries``)."""
        with self._lock:
            return len(self._dedup)

    def route(self, event: LifecycleEvent) -> None:
        """Deliver one `ops.*` event to every matching endpoint. Never raises."""
        try:
            self._route(event)
        except Exception as exc:  # noqa: BLE001 — must never block the source op
            logger.warning("ops alert routing failed: %s", exc)

    # -- internals ---------------------------------------------------------

    def _route(self, event: LifecycleEvent) -> None:
        if not self._config.endpoints:
            return
        type_value = event.type.value
        if type_value not in OPS_EVENT_TYPES:
            return
        severity = event.severity or EventSeverity.INFO
        matching = [
            ep for ep in self._config.endpoints if ep.accepts(type_value, severity)
        ]
        if not matching:
            return
        if self._suppressed(type_value, event.subject.ref):
            return
        record = sanitize_record(event.to_record())
        if self._config.sign_secret:
            record = sign_record(
                record, self._config.sign_secret, self._config.sign_keyid
            )
        for endpoint in matching:
            result = self._sinks[endpoint.endpoint_id].deliver(record)
            self._audit_attempt(endpoint, event, result)

    def _suppressed(self, type_value: str, subject_ref: str) -> bool:
        """At most one delivery per (event type, subject, window)."""
        key = (type_value, subject_ref)
        now = self._clock()
        with self._lock:
            last = self._dedup.get(key)
            if last is not None and (now - last) < self._config.dedup_window_s:
                self._dedup.move_to_end(key)
                return True
            self._dedup[key] = now
            self._dedup.move_to_end(key)
            while len(self._dedup) > self._config.dedup_max_entries:
                self._dedup.popitem(last=False)  # evict oldest
            return False

    def _audit_attempt(
        self,
        endpoint: AlertEndpoint,
        event: LifecycleEvent,
        result: DeliveryResult,
    ) -> None:
        """One hash-chained audit entry per attempted delivery (ADR-0192 D6).

        An audit-write failure is logged, never raised into the alerting path.
        """
        try:
            if self._audit is None:
                path = self._config.audit_log_path or AUDIT_LOG_PATH
                self._audit = AuditLog(path)
            details: dict[str, Any] = {
                "endpoint_id": endpoint.endpoint_id,
                "event_id": event.event_id,
                "event_type": event.type.value,
                "outcome": "delivered" if result.ok else "error",
                "attempts": result.attempts,
            }
            if result.error is not None:
                details["error"] = result.error
            self._audit.append(
                event_type=AuditEventType.ALERT_DELIVERY,
                actor="alert-router",
                resource_id=event.event_id,
                details=details,
            )
        except Exception as exc:  # noqa: BLE001 — audit must never break alerting
            logger.warning("alert delivery audit append failed: %s", exc)


# --------------------------------------------------------------------------- #
# Process-wide convenience (used by wired sources, e.g. server/quotas.py)
# --------------------------------------------------------------------------- #

_cache_lock = threading.Lock()
_router_cache: tuple[AlertsConfig, AlertRouter] | None = None


def _router_for(config: AlertsConfig) -> AlertRouter:
    """Reuse one router per config so the dedup window survives across calls."""
    global _router_cache
    with _cache_lock:
        if _router_cache is not None and _router_cache[0] == config:
            return _router_cache[1]
        router = AlertRouter(config)
        _router_cache = (config, router)
        return router


def _reset_router_cache() -> None:
    """Testing hook: drop the cached router (and its dedup state)."""
    global _router_cache
    with _cache_lock:
        _router_cache = None


def emit_ops_alert(
    *,
    event_type: EventType | str,
    severity: EventSeverity | str,
    subject_ref: str,
    subject_kind: SubjectKind | str = SubjectKind.OPS,
    subject_digest: str | None = None,
    payload: dict[str, Any] | None = None,
    source: str | None = None,
    background: bool = False,
) -> None:
    """Fire-and-forget `ops.*` alert for wired source code paths.

    No-op when no ``NOVA_ALERTS_*`` endpoint is configured (ADR-0192 D3:
    byte-identical to today — the event is then not emitted anywhere, not
    even to a configured events log). When configured, the event first rides
    the normal ADR-0137 emitter (durable ``events.jsonl`` record if the user
    enabled one), then the alert router. **Never raises**; with
    ``background=True`` delivery runs on a daemon thread so the source
    operation is never slowed by endpoint latency.
    """
    try:
        config = load_alerts_config_from_env()
        if not config.enabled:
            return
        event = LifecycleEvent(
            type=EventType(event_type),
            severity=EventSeverity(severity),
            subject=Subject(
                kind=SubjectKind(subject_kind),
                ref=subject_ref,
                digest=subject_digest,
            ),
            payload=payload or {},
            source=source,
            nova_version=_nova_version(),
        )
        build_emitter_from_env().emit(event)  # durable record; no-op unconfigured
        router = _router_for(config)
        if background:
            threading.Thread(
                target=router.route,
                args=(event,),
                daemon=True,
                name="nova-alert-delivery",
            ).start()
        else:
            router.route(event)
    except Exception as exc:  # noqa: BLE001 — must never block the source op
        logger.warning("ops alert emission failed: %s", exc)
