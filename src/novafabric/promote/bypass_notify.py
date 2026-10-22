"""Bypass notification dispatch — fires when a SoD bypass is created.

Channels come from the environment (:func:`build_notifier_from_env`) and are wired
into every ``PromoteBundleStore`` by default, so every bypass path — ``nova seal
bypass`` and ``POST /api/seal/{capsule_id}/bypass`` — notifies. Until audit H3
(2026-10-22) nothing called the factory and nothing was ever sent.

:func:`dispatch_bypass_event` is the one entry point. It never raises: a bypass is
an emergency action, and a down webhook must not block it. A failure is logged,
and every bypass event is recorded in the hash-chained audit log as
``seal.bypass.notify`` with outcome ``delivered``, ``failed`` or
``not_configured``, so a bypass nobody was told about is still on the record. A
webhook URL is recorded as ``scheme://host[:port]`` only, because its path or
query may carry a token.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: ``outcome`` values recorded in the ``seal.bypass.notify`` audit entry.
OUTCOME_DELIVERED = "delivered"
OUTCOME_FAILED = "failed"
OUTCOME_NOT_CONFIGURED = "not_configured"


# ---------------------------------------------------------------------------
# Event dataclass
# ---------------------------------------------------------------------------


@dataclass
class BypassEvent:
    event_type: str  # "bypass_created" (the only event emitted today)
    capsule_id: str
    bypass_uuid: str
    valid_until: str  # ISO-8601
    requester_keyid: str | None = None
    reason: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def as_dict(self) -> dict[str, object]:
        return {
            "event_type": self.event_type,
            "capsule_id": self.capsule_id,
            "bypass_uuid": self.bypass_uuid,
            "valid_until": self.valid_until,
            "requester_keyid": self.requester_keyid,
            "reason": self.reason,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Protocol and errors
# ---------------------------------------------------------------------------


class BypassNotifier(Protocol):
    def notify(self, event: BypassEvent) -> None: ...


class BypassNotifyError(Exception):
    """One or more bypass notification channels failed.

    ``failed_channels`` names them (``"file"``, ``"webhook:<scheme://host>"``); the
    message carries no URL path, query or credential.
    """

    def __init__(self, failed_channels: list[str], reasons: list[str]) -> None:
        self.failed_channels = failed_channels
        self.reasons = reasons
        super().__init__("; ".join(reasons))


def webhook_origin(url: str) -> str:
    """``scheme://host[:port]`` of *url*: safe to log (path, query, userinfo dropped)."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or "?"
        port = f":{parts.port}" if parts.port is not None else ""
    except ValueError:
        return "<unparseable URL>"
    return f"{parts.scheme or '?'}://{host}{port}"


# ---------------------------------------------------------------------------
# Null notifier (no channel configured)
# ---------------------------------------------------------------------------


class NullBypassNotifier:
    """No channel configured. :func:`dispatch_bypass_event` audits ``not_configured``."""

    def notify(self, event: BypassEvent) -> None:  # noqa: D102
        pass


# ---------------------------------------------------------------------------
# File notifier
# ---------------------------------------------------------------------------


class FileBypassNotifier:
    """Appends bypass events as JSON lines to an audit log file. Raises on failure."""

    def __init__(self, audit_log_path: Path) -> None:
        self._path = audit_log_path

    def notify(self, event: BypassEvent) -> None:
        line = json.dumps(event.as_dict(), ensure_ascii=False) + "\n"
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:
            raise BypassNotifyError(
                ["file"], [f"file: {type(exc).__name__}: {exc.strerror or exc}"]
            ) from None


# ---------------------------------------------------------------------------
# Webhook notifier
# ---------------------------------------------------------------------------


class WebhookBypassNotifier:
    """POSTs bypass events as JSON to a configured ``http(s)://`` URL.

    Raises :class:`BypassNotifyError` when the POST fails or the endpoint answers
    with an error status. :func:`dispatch_bypass_event` contains it, so a failure
    never blocks a bypass, but it is no longer swallowed where nobody sees it.
    """

    def __init__(self, url: str, timeout_s: float = 5.0) -> None:
        self._url = url
        self._timeout = timeout_s

    @property
    def url(self) -> str:
        return self._url

    def notify(self, event: BypassEvent) -> None:
        origin = webhook_origin(self._url)
        channel = [f"webhook:{origin}"]
        if urlsplit(self._url).scheme not in ("http", "https"):
            raise BypassNotifyError(channel, [f"webhook {origin}: only http(s) URLs are sent"])
        body = json.dumps(event.as_dict(), ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout):  # noqa: S310 — http(s) only
                pass
        except urllib.error.HTTPError as exc:
            raise BypassNotifyError(channel, [f"webhook {origin}: HTTP {exc.code}"]) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", None) or type(exc).__name__
            raise BypassNotifyError(channel, [f"webhook {origin}: {reason}"]) from None


# ---------------------------------------------------------------------------
# Multi notifier
# ---------------------------------------------------------------------------


class MultiBypassNotifier:
    """Fans out to every notifier, then raises one :class:`BypassNotifyError`
    naming each channel that failed. One failing channel never stops the others."""

    def __init__(self, notifiers: list[BypassNotifier]) -> None:
        self._notifiers = notifiers

    @property
    def notifiers(self) -> list[BypassNotifier]:
        return list(self._notifiers)

    def notify(self, event: BypassEvent) -> None:
        failed: list[str] = []
        reasons: list[str] = []
        for notifier in self._notifiers:
            try:
                notifier.notify(event)
            except BypassNotifyError as exc:
                failed.extend(exc.failed_channels)
                reasons.extend(exc.reasons)
            except Exception as exc:  # noqa: BLE001 — one channel never stops the rest
                failed.extend(channel_names(notifier))
                reasons.append(f"{type(notifier).__name__}: {type(exc).__name__}")
        if failed:
            raise BypassNotifyError(failed, reasons)


def channel_names(notifier: BypassNotifier) -> list[str]:
    """The configured channels of *notifier*, as recorded in the audit log."""
    if isinstance(notifier, MultiBypassNotifier):
        return [name for child in notifier.notifiers for name in channel_names(child)]
    if isinstance(notifier, FileBypassNotifier):
        return ["file"]
    if isinstance(notifier, WebhookBypassNotifier):
        return [f"webhook:{webhook_origin(notifier.url)}"]
    if isinstance(notifier, NullBypassNotifier):
        return []
    return [type(notifier).__name__]


# ---------------------------------------------------------------------------
# Factory: build from environment variables
# ---------------------------------------------------------------------------


def build_notifier_from_env() -> BypassNotifier:
    """Build the notifier from environment variables, read at call time.

    * ``NOVA_BYPASS_NOTIFY_FILE``    — path to append JSON-lines events to
    * ``NOVA_BYPASS_NOTIFY_WEBHOOK`` — ``http(s)://`` URL to POST events to

    Returns ``NullBypassNotifier`` when neither variable is set, and a
    ``MultiBypassNotifier`` when both are.
    """
    file_path = os.environ.get("NOVA_BYPASS_NOTIFY_FILE", "").strip()
    webhook_url = os.environ.get("NOVA_BYPASS_NOTIFY_WEBHOOK", "").strip()

    notifiers: list[BypassNotifier] = []
    if file_path:
        notifiers.append(FileBypassNotifier(Path(file_path).expanduser()))
    if webhook_url:
        notifiers.append(WebhookBypassNotifier(webhook_url))

    if len(notifiers) == 0:
        return NullBypassNotifier()
    if len(notifiers) == 1:
        return notifiers[0]
    return MultiBypassNotifier(notifiers)


# ---------------------------------------------------------------------------
# Dispatch: the one call every bypass path makes
# ---------------------------------------------------------------------------


def dispatch_bypass_event(notifier: BypassNotifier, event: BypassEvent) -> str:
    """Notify *event* through *notifier*; log and audit the outcome. Never raises.

    Returns the outcome: ``delivered``, ``failed`` or ``not_configured``.
    """
    channels = channel_names(notifier)
    failed: list[str] = []
    reasons: list[str] = []
    if not channels:
        outcome = OUTCOME_NOT_CONFIGURED
    else:
        try:
            notifier.notify(event)
            outcome = OUTCOME_DELIVERED
        except BypassNotifyError as exc:
            outcome, failed, reasons = OUTCOME_FAILED, exc.failed_channels, exc.reasons
        except Exception as exc:  # noqa: BLE001 — a bypass is never blocked by a notifier
            outcome, failed, reasons = OUTCOME_FAILED, channels, [type(exc).__name__]
    if outcome == OUTCOME_FAILED:
        logger.warning(
            "bypass notification failed: capsule=%s bypass=%s channels=%s",
            event.capsule_id,
            event.bypass_uuid,
            ", ".join(failed),
            extra={
                "capsule_id": event.capsule_id,
                "bypass_uuid": event.bypass_uuid,
                "failed_channels": failed,
                "reasons": reasons,
            },
        )
    _audit(event, outcome, channels, failed, reasons)
    return outcome


def _audit(
    event: BypassEvent,
    outcome: str,
    channels: list[str],
    failed: list[str],
    reasons: list[str],
) -> None:
    """Append one ``seal.bypass.notify`` entry; an audit fault is logged, never raised."""
    try:
        from novafabric.audit import (  # noqa: PLC0415
            AuditEventType,
            AuditLog,
            resolve_audit_log_path,
        )

        details: dict[str, object] = {
            "event_type": event.event_type,
            "bypass_uuid": event.bypass_uuid,
            "valid_until": event.valid_until,
            "outcome": outcome,
            "channels": channels,
        }
        if failed:
            details["failed_channels"] = failed
            details["errors"] = [r[:300] for r in reasons]
        AuditLog(resolve_audit_log_path()).append(
            event_type=AuditEventType.SEAL_BYPASS_NOTIFY,
            actor="bypass-notifier",
            resource_id=event.capsule_id,
            details=details,
        )
    except Exception as exc:  # noqa: BLE001 — auditing never blocks a bypass
        logger.warning(
            "bypass notification audit append failed: %s",
            type(exc).__name__,
            extra={"bypass_uuid": event.bypass_uuid},
        )
