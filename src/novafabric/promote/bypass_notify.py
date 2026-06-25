"""Bypass notification dispatch — fires when a SoD bypass is created or expires."""

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

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event dataclass
# ---------------------------------------------------------------------------


@dataclass
class BypassEvent:
    event_type: str  # "bypass_created" | "bypass_approved" | "bypass_expired"
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
# Protocol
# ---------------------------------------------------------------------------


class BypassNotifier(Protocol):
    def notify(self, event: BypassEvent) -> None: ...


# ---------------------------------------------------------------------------
# Null notifier (default)
# ---------------------------------------------------------------------------


class NullBypassNotifier:
    """No-op notifier — default when none configured."""

    def notify(self, event: BypassEvent) -> None:  # noqa: D102
        pass


# ---------------------------------------------------------------------------
# File notifier
# ---------------------------------------------------------------------------


class FileBypassNotifier:
    """Appends bypass events as JSON lines to an audit log file."""

    def __init__(self, audit_log_path: Path) -> None:
        self._path = audit_log_path

    def notify(self, event: BypassEvent) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.as_dict(), ensure_ascii=False) + "\n"
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line)


# ---------------------------------------------------------------------------
# Webhook notifier
# ---------------------------------------------------------------------------


class WebhookBypassNotifier:
    """POSTs bypass events as JSON to a configured URL.

    All exceptions are swallowed — notification failure must never block a
    bypass operation.
    """

    def __init__(self, url: str, timeout_s: float = 5.0) -> None:
        self._url = url
        self._timeout = timeout_s

    def notify(self, event: BypassEvent) -> None:
        try:
            body = json.dumps(event.as_dict(), ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                self._url,
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=self._timeout):
                pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("bypass webhook notify failed url=%r: %s", self._url, exc)


# ---------------------------------------------------------------------------
# Multi notifier
# ---------------------------------------------------------------------------


class MultiBypassNotifier:
    """Fans out to multiple notifiers; swallows per-notifier exceptions."""

    def __init__(self, notifiers: list[BypassNotifier]) -> None:
        self._notifiers = notifiers

    def notify(self, event: BypassEvent) -> None:
        for notifier in self._notifiers:
            try:
                notifier.notify(event)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "bypass notifier %r raised: %s", type(notifier).__name__, exc
                )


# ---------------------------------------------------------------------------
# Factory: build from environment variables
# ---------------------------------------------------------------------------


def build_notifier_from_env() -> BypassNotifier:
    """Build the appropriate notifier based on environment variables.

    * ``NOVA_BYPASS_NOTIFY_FILE``    — path to append JSON-lines audit log
    * ``NOVA_BYPASS_NOTIFY_WEBHOOK`` — HTTP(S) URL to POST events to

    Returns ``NullBypassNotifier`` when neither variable is set.
    Returns ``MultiBypassNotifier`` when both are set.
    """
    file_path = os.environ.get("NOVA_BYPASS_NOTIFY_FILE")
    webhook_url = os.environ.get("NOVA_BYPASS_NOTIFY_WEBHOOK")

    notifiers: list[BypassNotifier] = []
    if file_path:
        notifiers.append(FileBypassNotifier(Path(file_path)))
    if webhook_url:
        notifiers.append(WebhookBypassNotifier(webhook_url))

    if len(notifiers) == 0:
        return NullBypassNotifier()
    if len(notifiers) == 1:
        return notifiers[0]
    return MultiBypassNotifier(notifiers)
