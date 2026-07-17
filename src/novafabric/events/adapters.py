"""Notification render adapters — Slack / PagerDuty / email (ADR-0192 D5, experimental).

Adapters are **renderers, not integrations**: each takes the canonical,
already-hygiene-scanned event *record* (the same dict the webhook core POSTs)
and produces the target's payload shape over the SAME delivery core —

- **Slack**: incoming-webhook JSON (``text`` + minimal ``blocks``) to the
  user's webhook URL;
- **PagerDuty**: Events API v2 JSON (``routing_key`` supplied by config,
  severity mapped from :class:`EventSeverity`) to the user-configured endpoint
  URL — even well-known SaaS endpoints are config, never constants;
- **email**: an RFC 5322 message via stdlib :mod:`smtplib`/:mod:`email` to a
  user-configured SMTP relay (host/port/from/to). No bundled MTA, no default
  relay, no hardcoded URL.

Zero new dependencies: the webhook core already uses ``httpx``; Slack/PagerDuty
shapes are wire formats (not libraries); email is stdlib. The payload shapes
are **external contracts** — every shape is fixture-pinned in the tests.
"""

from __future__ import annotations

import json
import logging
import smtplib
from email.message import EmailMessage
from enum import Enum
from typing import Any

from novafabric.events.sinks import DeliveryResult

logger = logging.getLogger(__name__)


class Adapter(str, Enum):
    """Which renderer an alert endpoint uses. Default keeps slice-1 behavior."""

    WEBHOOK = "webhook"
    SLACK = "slack"
    PAGERDUTY = "pagerduty"
    EMAIL = "email"


#: PagerDuty Events API v2 severity is drawn from {critical, error, warning,
#: info}; NovaFabric's three map straight through (ADR-0192 D5).
PAGERDUTY_SEVERITY: dict[str, str] = {
    "info": "info",
    "warning": "warning",
    "critical": "critical",
}

_DEFAULT_PAGERDUTY_SEVERITY = "critical"
_DEFAULT_SEVERITY = "info"


def _fields(record: dict[str, Any]) -> tuple[str, str, str]:
    """Common (event_type, subject_ref, severity) triple from a record."""
    event_type = str(record.get("type", ""))
    subject = record.get("subject") or {}
    subject_ref = str(subject.get("ref", "")) if isinstance(subject, dict) else ""
    severity = record.get("severity") or _DEFAULT_SEVERITY
    return event_type, subject_ref, str(severity)


def render_slack(record: dict[str, Any]) -> dict[str, Any]:
    """Slack incoming-webhook payload: a fallback ``text`` plus minimal blocks."""
    event_type, subject_ref, severity = _fields(record)
    event_id = str(record.get("event_id", ""))
    occurred_at = str(record.get("occurred_at", ""))
    return {
        "text": f"[{severity}] {event_type} — {subject_ref}",
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{severity.upper()}: {event_type}",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Subject:* `{subject_ref}`"},
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"event `{event_id}` · {occurred_at}"}
                ],
            },
        ],
    }


def render_pagerduty(record: dict[str, Any], *, routing_key: str) -> dict[str, Any]:
    """PagerDuty Events API v2 ``trigger`` payload (``dedup_key`` = event id)."""
    event_type, subject_ref, _ = _fields(record)
    # Absent severity pages at critical (safest); present ones map straight.
    pd_severity = PAGERDUTY_SEVERITY.get(
        record.get("severity") or "", _DEFAULT_PAGERDUTY_SEVERITY
    )
    payload = record.get("payload")
    return {
        "routing_key": routing_key,
        "event_action": "trigger",
        "dedup_key": str(record.get("event_id", "")),
        "payload": {
            "summary": f"{event_type}: {subject_ref}",
            "source": record.get("source") or "novafabric",
            "severity": pd_severity,
            "component": subject_ref,
            "group": event_type,
            "class": event_type,
            "custom_details": payload if isinstance(payload, dict) else {},
        },
    }


def build_email_message(
    record: dict[str, Any], *, mail_from: str, mail_to: str
) -> EmailMessage:
    """Render the event as a plain-text RFC 5322 :class:`EmailMessage`."""
    event_type, subject_ref, severity = _fields(record)
    event_id = str(record.get("event_id", ""))
    occurred_at = str(record.get("occurred_at", ""))
    payload = record.get("payload")
    payload_obj = payload if isinstance(payload, dict) else {}

    msg = EmailMessage()
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg["Subject"] = f"[NovaFabric {severity}] {event_type} — {subject_ref}"
    msg["X-NovaFabric-Event-Id"] = event_id
    msg["X-NovaFabric-Event-Type"] = event_type
    if record.get("severity"):
        msg["X-NovaFabric-Severity"] = severity

    body = "\n".join([
        "NovaFabric operational alert",
        "",
        f"Event:     {event_type}",
        f"Severity:  {severity}",
        f"Subject:   {subject_ref}",
        f"Event ID:  {event_id}",
        f"Occurred:  {occurred_at}",
        f"Source:    {record.get('source') or '-'}",
        "",
        "Payload:",
        json.dumps(payload_obj, indent=2, sort_keys=True),
    ])
    msg.set_content(body)
    return msg


def send_email(
    msg: EmailMessage, *, host: str, port: int, timeout: float
) -> DeliveryResult:
    """Deliver one message to a user-configured SMTP relay. Fail-safe: never raises."""
    try:
        with smtplib.SMTP(host, port, timeout=timeout) as smtp:
            smtp.send_message(msg)
        return DeliveryResult(ok=True, attempts=1)
    except Exception as exc:  # noqa: BLE001 — fail-safe, mirrors WebhookSink
        logger.warning(
            "alert email delivery failed host=%r port=%d: %s", host, port, exc
        )
        return DeliveryResult(ok=False, attempts=1, error=str(exc))
