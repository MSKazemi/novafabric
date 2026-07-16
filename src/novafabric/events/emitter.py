"""Lifecycle event emitter: opt-in config, hygiene, signing, dispatch (ADR-0137).

Strictly opt-in: nothing is emitted until the user names a sink through the
``NOVA_EVENTS_*`` environment variables (following the ``NOVA_BYPASS_NOTIFY_*``
convention). There is no default destination and no silent telemetry.

Environment variables
---------------------
``NOVA_EVENTS_LOG``
    Path of the local append-only ``events.jsonl`` (enables the file sink).
``NOVA_EVENTS_WEBHOOK``
    One or more webhook URLs, comma-separated (enables the webhook sink).
    User-configured only — NovaFabric ships with NO default destination.
``NOVA_EVENTS_MAX_RETRIES``
    Bounded webhook retry count (default 2).
``NOVA_EVENTS_TIMEOUT_S``
    Per-request webhook timeout in seconds (default 5.0).
``NOVA_EVENTS_SIGN_SECRET``
    Shared HMAC-SHA256 secret; enables signing. Never written to config,
    the log, or any payload.
``NOVA_EVENTS_SIGN_KEYID``
    Key identifier placed in ``signature.keyid`` (default ``"default"``).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from novafabric.events.hygiene import sanitize_record
from novafabric.events.model import EventType, LifecycleEvent, Subject, SubjectKind
from novafabric.events.signing import sign_record
from novafabric.events.sinks import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_S,
    EventSink,
    FileSink,
    MultiSink,
    NullSink,
    WebhookSink,
)

logger = logging.getLogger(__name__)

ENV_LOG = "NOVA_EVENTS_LOG"
ENV_WEBHOOK = "NOVA_EVENTS_WEBHOOK"
ENV_MAX_RETRIES = "NOVA_EVENTS_MAX_RETRIES"
ENV_TIMEOUT_S = "NOVA_EVENTS_TIMEOUT_S"
ENV_SIGN_SECRET = "NOVA_EVENTS_SIGN_SECRET"
ENV_SIGN_KEYID = "NOVA_EVENTS_SIGN_KEYID"


@dataclass(frozen=True)
class EventsConfig:
    """Resolved emitter configuration (all user-supplied; no defaults fire)."""

    log_path: Path | None = None
    webhook_urls: tuple[str, ...] = ()
    max_retries: int = DEFAULT_MAX_RETRIES
    timeout_s: float = DEFAULT_TIMEOUT_S
    sign_secret: bytes | None = None
    sign_keyid: str = "default"

    @property
    def enabled(self) -> bool:
        return self.log_path is not None or bool(self.webhook_urls)


def _int_env(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("ignoring non-integer %s=%r", key, raw)
        return default


def _float_env(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("ignoring non-numeric %s=%r", key, raw)
        return default


def load_config_from_env(env: Mapping[str, str] | None = None) -> EventsConfig:
    """Build the emitter configuration from ``NOVA_EVENTS_*`` variables."""
    env = os.environ if env is None else env
    log_raw = env.get(ENV_LOG, "").strip()
    webhook_raw = env.get(ENV_WEBHOOK, "").strip()
    urls = tuple(u.strip() for u in webhook_raw.split(",") if u.strip())
    secret_raw = env.get(ENV_SIGN_SECRET, "")
    keyid = env.get(ENV_SIGN_KEYID, "").strip()
    if keyid and not secret_raw:
        # Fail closed on signing only: emit unsigned, warn (spec edge case).
        logger.warning(
            "%s is set but %s is empty — events will be emitted UNSIGNED",
            ENV_SIGN_KEYID,
            ENV_SIGN_SECRET,
        )
    return EventsConfig(
        log_path=Path(log_raw) if log_raw else None,
        webhook_urls=urls,
        max_retries=_int_env(env, ENV_MAX_RETRIES, DEFAULT_MAX_RETRIES),
        timeout_s=_float_env(env, ENV_TIMEOUT_S, DEFAULT_TIMEOUT_S),
        sign_secret=secret_raw.encode("utf-8") if secret_raw else None,
        sign_keyid=keyid or "default",
    )


def build_sink(config: EventsConfig) -> EventSink:
    """NullSink when nothing configured; MultiSink over every named sink."""
    sinks: list[EventSink] = []
    if config.log_path is not None:
        sinks.append(FileSink(config.log_path))
    for url in config.webhook_urls:
        sinks.append(
            WebhookSink(
                url, timeout_s=config.timeout_s, max_retries=config.max_retries
            )
        )
    if not sinks:
        return NullSink()
    if len(sinks) == 1:
        return sinks[0]
    return MultiSink(sinks)


def _nova_version() -> str | None:
    try:
        return version("novafabric")
    except PackageNotFoundError:
        return None


class LifecycleEventEmitter:
    """Serializes, sanitizes, optionally signs, and dispatches events.

    ``emit`` is fail-safe by construction: every failure — hygiene, signing,
    or sink — is logged and swallowed, never raised into the workload path
    (ADR-0137 D4 hard invariant).
    """

    def __init__(
        self,
        sink: EventSink,
        *,
        sign_secret: bytes | None = None,
        sign_keyid: str = "default",
    ) -> None:
        self._sink = sink
        self._sign_secret = sign_secret
        self._sign_keyid = sign_keyid

    @property
    def enabled(self) -> bool:
        return not isinstance(self._sink, NullSink)

    def emit(self, event: LifecycleEvent) -> None:
        if not self.enabled:
            return
        try:
            record = sanitize_record(event.to_record())
            if self._sign_secret:
                record = sign_record(record, self._sign_secret, self._sign_keyid)
            self._sink.send(record)
        except Exception as exc:  # noqa: BLE001 — must never block the workload
            logger.warning("lifecycle event emission failed: %s", exc)


def build_emitter_from_env(
    env: Mapping[str, str] | None = None,
) -> LifecycleEventEmitter:
    config = load_config_from_env(env)
    return LifecycleEventEmitter(
        build_sink(config),
        sign_secret=config.sign_secret,
        sign_keyid=config.sign_keyid,
    )


def emit_lifecycle_event(
    *,
    event_type: EventType | str,
    subject_kind: SubjectKind | str,
    subject_ref: str,
    subject_digest: str | None = None,
    payload: dict[str, Any] | None = None,
    source: str | None = None,
) -> None:
    """Fire-and-forget convenience for workload code paths.

    No-op when no sink is configured (the opt-in default); **never raises** —
    any error, including invalid arguments, is logged and swallowed so a
    lifecycle transition can never be blocked by its own event emission.
    """
    try:
        emitter = build_emitter_from_env()
        if not emitter.enabled:
            return
        event = LifecycleEvent(
            type=EventType(event_type),
            subject=Subject(
                kind=SubjectKind(subject_kind),
                ref=subject_ref,
                digest=subject_digest,
            ),
            payload=payload or {},
            source=source,
            nova_version=_nova_version(),
        )
        emitter.emit(event)
    except Exception as exc:  # noqa: BLE001 — must never block the workload
        logger.warning("lifecycle event emission failed: %s", exc)
