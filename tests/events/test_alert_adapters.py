"""ADR-0192 slice 2: Slack / PagerDuty / email render adapters (experimental).

Adapters are **renderers, not integrations** (ADR-0192 D5): each takes the
canonical, already-hygiene-scanned event record and produces the target's
payload shape over the SAME webhook / delivery core. The payload shapes are
external contracts, so every shape is fixture-pinned here.
"""

from __future__ import annotations

import email
import email.header
import json
from pathlib import Path
from typing import Any

import pytest

from novafabric.events.adapters import (
    PAGERDUTY_SEVERITY,
    Adapter,
    build_email_message,
    render_pagerduty,
    render_slack,
    send_email,
)
from novafabric.events.alerts import (
    AlertEndpoint,
    AlertRouter,
    AlertsConfig,
    load_alerts_config_from_env,
)
from novafabric.events.model import (
    EventSeverity,
    EventType,
    LifecycleEvent,
    Subject,
    SubjectKind,
)
from novafabric.events.sinks import DeliveryResult


def _record(
    *,
    event_type: EventType = EventType.OPS_QUOTA_BREACHED,
    severity: EventSeverity | None = EventSeverity.CRITICAL,
    ref: str = "quota:capsules",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return LifecycleEvent(
        type=event_type,
        severity=severity,
        subject=Subject(kind=SubjectKind.OPS, ref=ref),
        payload=payload if payload is not None else {"kind": "capsules", "usage": 10, "limit": 10},
        source="nova server",
    ).to_record()


# --------------------------------------------------------------------------- #
# 1. Slack incoming-webhook JSON (text + minimal blocks)
# --------------------------------------------------------------------------- #


class TestSlackRender:
    def test_shape_is_pinned(self) -> None:
        rec = _record()
        payload = render_slack(rec)
        assert payload["text"] == "[critical] ops.quota.breached — quota:capsules"
        blocks = payload["blocks"]
        assert isinstance(blocks, list)
        assert blocks[0] == {
            "type": "header",
            "text": {"type": "plain_text", "text": "CRITICAL: ops.quota.breached", "emoji": True},
        }
        assert blocks[1] == {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Subject:* `quota:capsules`"},
        }
        # context block carries the event id + timestamp for dedup on the receiver
        ctx = blocks[2]
        assert ctx["type"] == "context"
        assert rec["event_id"] in ctx["elements"][0]["text"]
        assert rec["occurred_at"] in ctx["elements"][0]["text"]

    def test_json_serialisable(self) -> None:
        # must be a plain JSON body for the webhook core
        json.dumps(render_slack(_record()))

    def test_missing_severity_defaults_to_info(self) -> None:
        rec = _record(event_type=EventType.OPS_DRIFT_DETECTED, severity=None)
        payload = render_slack(rec)
        assert payload["text"].startswith("[info] ")
        assert payload["blocks"][0]["text"]["text"] == "INFO: ops.drift.detected"


# --------------------------------------------------------------------------- #
# 2. PagerDuty Events API v2 JSON
# --------------------------------------------------------------------------- #


class TestPagerDutyRender:
    def test_v2_shape_is_pinned(self) -> None:
        rec = _record()
        payload = render_pagerduty(rec, routing_key="R0UT1NGK3Y")
        assert payload["routing_key"] == "R0UT1NGK3Y"
        assert payload["event_action"] == "trigger"
        assert payload["dedup_key"] == rec["event_id"]
        body = payload["payload"]
        assert body["summary"] == "ops.quota.breached: quota:capsules"
        assert body["source"] == "nova server"
        assert body["severity"] == "critical"
        assert body["component"] == "quota:capsules"
        assert body["group"] == "ops.quota.breached"
        assert body["custom_details"] == {"kind": "capsules", "usage": 10, "limit": 10}

    def test_json_serialisable(self) -> None:
        json.dumps(render_pagerduty(_record(), routing_key="k"))

    def test_source_defaults_when_absent(self) -> None:
        rec = _record()
        rec.pop("source", None)
        assert render_pagerduty(rec, routing_key="k")["payload"]["source"] == "novafabric"

    @pytest.mark.parametrize(
        ("sev", "expected"),
        [("info", "info"), ("warning", "warning"), ("critical", "critical")],
    )
    def test_severity_mapping(self, sev: str, expected: str) -> None:
        assert PAGERDUTY_SEVERITY[sev] == expected
        rec = _record(severity=EventSeverity(sev))
        assert render_pagerduty(rec, routing_key="k")["payload"]["severity"] == expected

    def test_missing_severity_maps_to_critical(self) -> None:
        rec = _record(severity=None)
        assert render_pagerduty(rec, routing_key="k")["payload"]["severity"] == "critical"


# --------------------------------------------------------------------------- #
# 3. Email (stdlib smtplib/email) — RFC 5322
# --------------------------------------------------------------------------- #


class TestEmailBuild:
    def test_headers_and_body_pinned(self) -> None:
        rec = _record()
        msg = build_email_message(rec, mail_from="nova@ops.internal", mail_to="oncall@ops.internal")
        assert msg["From"] == "nova@ops.internal"
        assert msg["To"] == "oncall@ops.internal"
        assert msg["Subject"] == "[NovaFabric critical] ops.quota.breached — quota:capsules"
        assert msg["X-NovaFabric-Event-Id"] == rec["event_id"]
        assert msg["X-NovaFabric-Event-Type"] == "ops.quota.breached"
        assert msg["X-NovaFabric-Severity"] == "critical"
        body = msg.get_content()
        assert "ops.quota.breached" in body
        assert "quota:capsules" in body
        assert '"usage": 10' in body

    def test_is_a_valid_rfc5322_message(self) -> None:
        rec = _record()
        msg = build_email_message(rec, mail_from="a@b.c", mail_to="d@e.f")
        raw = msg.as_bytes()
        parsed = email.message_from_bytes(raw)
        # The em-dash makes the Subject a non-ASCII RFC 2047 encoded-word on the
        # wire; decode it back before comparing.
        decoded = str(email.header.make_header(email.header.decode_header(parsed["Subject"])))
        assert decoded == msg["Subject"]
        assert parsed["From"] == "a@b.c"
        assert not parsed.is_multipart()


class _FakeSMTP:
    instances: list[_FakeSMTP] = []

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sent: list[Any] = []
        _FakeSMTP.instances.append(self)

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def send_message(self, msg: Any) -> None:
        self.sent.append(msg)


class TestEmailSend:
    def test_send_email_uses_configured_relay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _FakeSMTP.instances = []
        import novafabric.events.adapters as adapters_mod

        monkeypatch.setattr(adapters_mod.smtplib, "SMTP", _FakeSMTP)
        rec = _record()
        msg = build_email_message(rec, mail_from="a@b.c", mail_to="d@e.f")
        result = send_email(msg, host="mail.internal", port=2525, timeout=3.0)
        assert isinstance(result, DeliveryResult)
        assert result.ok is True
        assert result.attempts == 1
        assert _FakeSMTP.instances[0].host == "mail.internal"
        assert _FakeSMTP.instances[0].port == 2525
        assert _FakeSMTP.instances[0].sent == [msg]

    def test_send_email_is_fail_safe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*a: Any, **k: Any) -> Any:
            raise OSError("relay unreachable")

        import novafabric.events.adapters as adapters_mod

        monkeypatch.setattr(adapters_mod.smtplib, "SMTP", _boom)
        msg = build_email_message(_record(), mail_from="a@b.c", mail_to="d@e.f")
        result = send_email(msg, host="x", port=25, timeout=1.0)  # must not raise
        assert result.ok is False
        assert result.error is not None


# --------------------------------------------------------------------------- #
# 4. Adapter selection wired into AlertEndpoint config
# --------------------------------------------------------------------------- #


def _config(*endpoints: AlertEndpoint, audit_path: Path) -> AlertsConfig:
    return AlertsConfig(
        endpoints=tuple(endpoints),
        max_retries=0,
        timeout_s=2.0,
        audit_log_path=audit_path,
    )


class TestAdapterSelection:
    def test_default_adapter_is_webhook(self) -> None:
        ep = AlertEndpoint(endpoint_id="e", url="http://a/h")
        assert ep.adapter is Adapter.WEBHOOK

    def test_env_adapter_positional(self) -> None:
        config = load_alerts_config_from_env({
            "NOVA_ALERTS_WEBHOOK": "http://a/h,http://b/h",
            "NOVA_ALERTS_ADAPTER": "slack,pagerduty",
        })
        assert [e.adapter for e in config.endpoints] == [Adapter.SLACK, Adapter.PAGERDUTY]

    def test_env_single_adapter_applies_to_all(self) -> None:
        config = load_alerts_config_from_env({
            "NOVA_ALERTS_WEBHOOK": "http://a/h,http://b/h",
            "NOVA_ALERTS_ADAPTER": "slack",
        })
        assert all(e.adapter is Adapter.SLACK for e in config.endpoints)

    def test_unknown_adapter_falls_back_to_webhook(self) -> None:
        config = load_alerts_config_from_env({
            "NOVA_ALERTS_WEBHOOK": "http://a/h",
            "NOVA_ALERTS_ADAPTER": "telegram",
        })
        assert config.endpoints[0].adapter is Adapter.WEBHOOK

    def test_pagerduty_and_smtp_env_populate_endpoint(self) -> None:
        config = load_alerts_config_from_env({
            "NOVA_ALERTS_WEBHOOK": "http://pd/v2",
            "NOVA_ALERTS_ADAPTER": "pagerduty",
            "NOVA_ALERTS_PAGERDUTY_ROUTING_KEY": "R0UT1NG",
            "NOVA_ALERTS_SMTP_HOST": "mail.internal",
            "NOVA_ALERTS_SMTP_PORT": "2525",
            "NOVA_ALERTS_EMAIL_FROM": "nova@ops",
            "NOVA_ALERTS_EMAIL_TO": "oncall@ops",
        })
        ep = config.endpoints[0]
        assert ep.routing_key == "R0UT1NG"
        assert ep.smtp_host == "mail.internal"
        assert ep.smtp_port == 2525
        assert ep.mail_from == "nova@ops"
        assert ep.mail_to == "oncall@ops"

    def test_slack_adapter_delivers_slack_payload(
        self, tmp_path: Path, webhook_server: Any
    ) -> None:
        router = AlertRouter(
            _config(
                AlertEndpoint(
                    endpoint_id="slack-1",
                    url=webhook_server.url,
                    min_severity=EventSeverity.INFO,
                    adapter=Adapter.SLACK,
                ),
                audit_path=tmp_path / "a.jsonl",
            )
        )
        router.route(
            LifecycleEvent(
                type=EventType.OPS_QUOTA_BREACHED,
                severity=EventSeverity.CRITICAL,
                subject=Subject(kind=SubjectKind.OPS, ref="quota:capsules"),
                payload={"kind": "capsules"},
            )
        )
        assert len(webhook_server.received) == 1
        body = json.loads(webhook_server.received[0]["body"])
        assert body["text"].startswith("[critical] ops.quota.breached")
        assert body["blocks"][0]["type"] == "header"

    def test_pagerduty_adapter_delivers_v2_payload(
        self, tmp_path: Path, webhook_server: Any
    ) -> None:
        router = AlertRouter(
            _config(
                AlertEndpoint(
                    endpoint_id="pagerduty-1",
                    url=webhook_server.url,
                    min_severity=EventSeverity.INFO,
                    adapter=Adapter.PAGERDUTY,
                    routing_key="R0UT1NG",
                ),
                audit_path=tmp_path / "a.jsonl",
            )
        )
        router.route(
            LifecycleEvent(
                type=EventType.OPS_SEAL_VERIFY_FAILED,
                severity=EventSeverity.WARNING,
                subject=Subject(kind=SubjectKind.OPS, ref="seal:run-abc"),
                payload={"reason": "bad_signature"},
            )
        )
        assert len(webhook_server.received) == 1
        body = json.loads(webhook_server.received[0]["body"])
        assert body["routing_key"] == "R0UT1NG"
        assert body["event_action"] == "trigger"
        assert body["payload"]["severity"] == "warning"

    def test_email_adapter_sends_via_relay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _FakeSMTP.instances = []
        import novafabric.events.adapters as adapters_mod

        monkeypatch.setattr(adapters_mod.smtplib, "SMTP", _FakeSMTP)
        audit_path = tmp_path / "a.jsonl"
        router = AlertRouter(
            _config(
                AlertEndpoint(
                    endpoint_id="email-1",
                    url="",  # unused for email
                    min_severity=EventSeverity.INFO,
                    adapter=Adapter.EMAIL,
                    smtp_host="mail.internal",
                    smtp_port=2525,
                    mail_from="nova@ops",
                    mail_to="oncall@ops",
                ),
                audit_path=audit_path,
            )
        )
        router.route(
            LifecycleEvent(
                type=EventType.OPS_BACKUP_FAILED,
                severity=EventSeverity.CRITICAL,
                subject=Subject(kind=SubjectKind.OPS, ref="backup:nightly"),
                payload={"job": "nightly"},
            )
        )
        assert len(_FakeSMTP.instances) == 1
        sent = _FakeSMTP.instances[0].sent[0]
        assert sent["Subject"] == "[NovaFabric critical] ops.backup.failed — backup:nightly"
        # the delivery was audited
        from novafabric.audit import AuditLog

        entries = AuditLog(audit_path).query()
        assert len(entries) == 1
        assert entries[0].details["outcome"] == "delivered"
        assert entries[0].details["endpoint_id"] == "email-1"
