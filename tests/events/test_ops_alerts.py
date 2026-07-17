"""ADR-0192 first slice: `ops.*` taxonomy, AlertRouter, quota wiring, audit trail.

Contract: design/adr/0192-alerting-notification-bus.md (accepted) —
- no config ⇒ byte-identical no-op (nothing emitted, nothing sent);
- per-rule dedup window: at most one delivery per (type, subject, window),
  bounded map with eviction (ADR-0179 bucket-map discipline);
- severity routing: per-endpoint minimum severity;
- fail-safe: an unreachable endpoint never fails/slows the source operation;
- HMAC signing rides the existing ADR-0137 signing path;
- hygiene: a seeded secret never leaves the process;
- one hash-chained audit entry per attempted delivery.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError

from novafabric.audit import AuditEntry, AuditLog
from novafabric.events import alerts as alerts_mod
from novafabric.events.alerts import (
    DEFAULT_DEDUP_MAX_ENTRIES,
    DEFAULT_DEDUP_WINDOW_S,
    OPS_EVENT_TYPES,
    AlertEndpoint,
    AlertRouter,
    AlertsConfig,
    emit_ops_alert,
    load_alerts_config_from_env,
)
from novafabric.events.model import (
    EventSeverity,
    EventType,
    LifecycleEvent,
    Subject,
    SubjectKind,
)
from novafabric.events.signing import verify_record

SCHEMA_PATH = (
    Path(__file__).parents[2] / "src/novafabric/schemas/lifecycle-event.schema.json"
)

OPS_SIX = {
    "ops.quota.breached",
    "ops.rate_limit.sustained",
    "ops.policy.violation",
    "ops.drift.detected",
    "ops.seal.verify_failed",
    "ops.backup.failed",
}


@pytest.fixture(autouse=True)
def _fresh_router_cache() -> Iterator[None]:
    alerts_mod._reset_router_cache()
    yield
    alerts_mod._reset_router_cache()


def _ops_event(
    *,
    event_type: EventType = EventType.OPS_QUOTA_BREACHED,
    severity: EventSeverity | None = EventSeverity.CRITICAL,
    ref: str = "quota:capsules",
    payload: dict[str, Any] | None = None,
) -> LifecycleEvent:
    return LifecycleEvent(
        type=event_type,
        severity=severity,
        subject=Subject(kind=SubjectKind.OPS, ref=ref),
        payload=payload if payload is not None else {"usage": 10, "limit": 10},
        source="nova server",
    )


def _endpoint(
    url: str,
    *,
    endpoint_id: str = "webhook-1",
    min_severity: EventSeverity = EventSeverity.INFO,
    event_types: frozenset[str] | None = None,
) -> AlertEndpoint:
    return AlertEndpoint(
        endpoint_id=endpoint_id,
        url=url,
        min_severity=min_severity,
        event_types=event_types or OPS_EVENT_TYPES,
    )


def _config(
    *endpoints: AlertEndpoint,
    audit_path: Path,
    dedup_window_s: float = DEFAULT_DEDUP_WINDOW_S,
    dedup_max_entries: int = DEFAULT_DEDUP_MAX_ENTRIES,
    max_retries: int = 0,
    timeout_s: float = 2.0,
    sign_secret: bytes | None = None,
    sign_keyid: str = "default",
) -> AlertsConfig:
    return AlertsConfig(
        endpoints=tuple(endpoints),
        dedup_window_s=dedup_window_s,
        dedup_max_entries=dedup_max_entries,
        max_retries=max_retries,
        timeout_s=timeout_s,
        sign_secret=sign_secret,
        sign_keyid=sign_keyid,
        audit_log_path=audit_path,
    )


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --------------------------------------------------------------------------- #
# 1. Taxonomy + severity field (additive)
# --------------------------------------------------------------------------- #


class TestOpsTaxonomy:
    def test_six_ops_types_exist(self) -> None:
        assert OPS_SIX <= {t.value for t in EventType}
        assert OPS_EVENT_TYPES == frozenset(OPS_SIX)

    def test_severity_defaults_to_none_and_is_omitted(self) -> None:
        """Non-ops events are unchanged — golden fixtures stay valid."""
        event = LifecycleEvent(
            type=EventType.CAPSULE_CREATED,
            subject=Subject(kind=SubjectKind.CAPSULE, ref="run-abc"),
        )
        assert event.severity is None
        assert "severity" not in event.to_record()

    def test_ops_record_carries_severity_and_validates(self) -> None:
        record = _ops_event(severity=EventSeverity.CRITICAL).to_record()
        assert record["severity"] == "critical"
        assert record["type"] == "ops.quota.breached"
        assert record["subject"]["kind"] == "ops"
        jsonschema.validate(
            record,
            json.loads(SCHEMA_PATH.read_text()),
            format_checker=jsonschema.FormatChecker(),
        )

    def test_invalid_severity_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LifecycleEvent(
                type=EventType.OPS_QUOTA_BREACHED,
                severity="urgent",  # type: ignore[arg-type]
                subject=Subject(kind=SubjectKind.OPS, ref="quota:capsules"),
            )


# --------------------------------------------------------------------------- #
# 2. Config parsing (NOVA_ALERTS_* — same conventions as NOVA_EVENTS_*)
# --------------------------------------------------------------------------- #


class TestAlertsConfigFromEnv:
    def test_no_env_disabled(self) -> None:
        config = load_alerts_config_from_env({})
        assert config.enabled is False
        assert config.endpoints == ()

    def test_urls_and_defaults(self) -> None:
        config = load_alerts_config_from_env(
            {"NOVA_ALERTS_WEBHOOK": "http://a.internal/h, http://b.internal/h"}
        )
        assert config.enabled is True
        assert [e.url for e in config.endpoints] == [
            "http://a.internal/h",
            "http://b.internal/h",
        ]
        assert [e.endpoint_id for e in config.endpoints] == ["webhook-1", "webhook-2"]
        assert all(e.min_severity == EventSeverity.WARNING for e in config.endpoints)
        assert all(e.event_types == OPS_EVENT_TYPES for e in config.endpoints)
        assert config.dedup_window_s == DEFAULT_DEDUP_WINDOW_S
        assert config.dedup_max_entries == DEFAULT_DEDUP_MAX_ENTRIES

    def test_per_endpoint_min_severity_positional(self) -> None:
        config = load_alerts_config_from_env({
            "NOVA_ALERTS_WEBHOOK": "http://a/h,http://b/h",
            "NOVA_ALERTS_MIN_SEVERITY": "info,critical",
        })
        assert config.endpoints[0].min_severity == EventSeverity.INFO
        assert config.endpoints[1].min_severity == EventSeverity.CRITICAL

    def test_single_min_severity_applies_to_all(self) -> None:
        config = load_alerts_config_from_env({
            "NOVA_ALERTS_WEBHOOK": "http://a/h,http://b/h",
            "NOVA_ALERTS_MIN_SEVERITY": "critical",
        })
        assert all(e.min_severity == EventSeverity.CRITICAL for e in config.endpoints)

    def test_types_allowlist_filters_unknown(self) -> None:
        config = load_alerts_config_from_env({
            "NOVA_ALERTS_WEBHOOK": "http://a/h",
            "NOVA_ALERTS_TYPES": "ops.quota.breached, capsule.created, not.a.type",
        })
        assert config.endpoints[0].event_types == frozenset({"ops.quota.breached"})

    def test_sign_secret_falls_back_to_events_secret(self) -> None:
        config = load_alerts_config_from_env({
            "NOVA_ALERTS_WEBHOOK": "http://a/h",
            "NOVA_EVENTS_SIGN_SECRET": "shared",
            "NOVA_EVENTS_SIGN_KEYID": "ci",
        })
        assert config.sign_secret == b"shared"
        assert config.sign_keyid == "ci"

    def test_alerts_sign_secret_overrides_events(self) -> None:
        config = load_alerts_config_from_env({
            "NOVA_ALERTS_WEBHOOK": "http://a/h",
            "NOVA_EVENTS_SIGN_SECRET": "shared",
            "NOVA_ALERTS_SIGN_SECRET": "alerts-only",
            "NOVA_ALERTS_SIGN_KEYID": "ops-1",
        })
        assert config.sign_secret == b"alerts-only"
        assert config.sign_keyid == "ops-1"

    def test_invalid_numbers_fall_back(self) -> None:
        config = load_alerts_config_from_env({
            "NOVA_ALERTS_WEBHOOK": "http://a/h",
            "NOVA_ALERTS_DEDUP_WINDOW_S": "soon",
            "NOVA_ALERTS_DEDUP_MAX_ENTRIES": "many",
            "NOVA_ALERTS_MAX_RETRIES": "lots",
        })
        assert config.dedup_window_s == DEFAULT_DEDUP_WINDOW_S
        assert config.dedup_max_entries == DEFAULT_DEDUP_MAX_ENTRIES
        assert config.max_retries == 2


# --------------------------------------------------------------------------- #
# 3. No config ⇒ byte-identical no-op
# --------------------------------------------------------------------------- #


class TestNoConfigNoOp:
    def test_router_without_endpoints_is_noop(self, tmp_path: Path) -> None:
        audit_path = tmp_path / "audit.jsonl"
        router = AlertRouter(_config(audit_path=audit_path))
        assert router.enabled is False
        router.route(_ops_event())
        assert not audit_path.exists()

    def test_emit_ops_alert_unconfigured_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        emit_ops_alert(
            event_type="ops.quota.breached",
            severity="critical",
            subject_ref="quota:capsules",
        )
        assert list(tmp_path.iterdir()) == []

    def test_unconfigured_alerts_skip_even_the_events_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR-0192 D3: no [alerts] block ⇒ byte-identical, even for users who
        already configured the ADR-0137 events log."""
        log = tmp_path / "events.jsonl"
        monkeypatch.setenv("NOVA_EVENTS_LOG", str(log))
        emit_ops_alert(
            event_type="ops.quota.breached",
            severity="critical",
            subject_ref="quota:capsules",
        )
        assert not log.exists()


# --------------------------------------------------------------------------- #
# 4. Dedup window + bounded map
# --------------------------------------------------------------------------- #


class TestDedupWindow:
    def test_n_breaches_one_window_one_delivery(
        self, tmp_path: Path, webhook_server: Any
    ) -> None:
        clock = FakeClock()
        router = AlertRouter(
            _config(_endpoint(webhook_server.url), audit_path=tmp_path / "a.jsonl"),
            clock=clock,
        )
        for _ in range(5):
            router.route(_ops_event())
        assert len(webhook_server.received) == 1

    def test_window_expiry_allows_redelivery(
        self, tmp_path: Path, webhook_server: Any
    ) -> None:
        clock = FakeClock()
        router = AlertRouter(
            _config(
                _endpoint(webhook_server.url),
                audit_path=tmp_path / "a.jsonl",
                dedup_window_s=60.0,
            ),
            clock=clock,
        )
        router.route(_ops_event())
        clock.now += 61.0
        router.route(_ops_event())
        assert len(webhook_server.received) == 2

    def test_distinct_subjects_not_deduped(
        self, tmp_path: Path, webhook_server: Any
    ) -> None:
        router = AlertRouter(
            _config(_endpoint(webhook_server.url), audit_path=tmp_path / "a.jsonl"),
            clock=FakeClock(),
        )
        router.route(_ops_event(ref="quota:capsules"))
        router.route(_ops_event(ref="quota:bytes"))
        assert len(webhook_server.received) == 2

    def test_dedup_map_is_bounded_with_eviction(
        self, tmp_path: Path, webhook_server: Any
    ) -> None:
        """Exceeding max entries evicts the oldest key (ADR-0179 discipline)."""
        clock = FakeClock()
        router = AlertRouter(
            _config(
                _endpoint(webhook_server.url),
                audit_path=tmp_path / "a.jsonl",
                dedup_max_entries=2,
            ),
            clock=clock,
        )
        for ref in ("quota:r1", "quota:r2", "quota:r3"):
            router.route(_ops_event(ref=ref))
        assert len(webhook_server.received) == 3
        assert router.dedup_entries <= 2
        # r1 was evicted, so a repeat inside the window delivers again.
        router.route(_ops_event(ref="quota:r1"))
        assert len(webhook_server.received) == 4
        # A still-tracked key stays suppressed.
        router.route(_ops_event(ref="quota:r3"))
        assert len(webhook_server.received) == 4


# --------------------------------------------------------------------------- #
# 5. Severity routing
# --------------------------------------------------------------------------- #


class TestSeverityRouting:
    def test_below_minimum_not_delivered(
        self, tmp_path: Path, webhook_server: Any
    ) -> None:
        audit_path = tmp_path / "a.jsonl"
        router = AlertRouter(
            _config(
                _endpoint(webhook_server.url, min_severity=EventSeverity.WARNING),
                audit_path=audit_path,
            )
        )
        router.route(_ops_event(severity=EventSeverity.INFO))
        assert webhook_server.received == []
        assert not audit_path.exists()  # no attempt ⇒ no audit entry

    def test_meets_minimum_delivered(
        self, tmp_path: Path, webhook_server: Any
    ) -> None:
        router = AlertRouter(
            _config(
                _endpoint(webhook_server.url, min_severity=EventSeverity.WARNING),
                audit_path=tmp_path / "a.jsonl",
            )
        )
        router.route(_ops_event(severity=EventSeverity.WARNING))
        assert len(webhook_server.received) == 1

    def test_per_endpoint_minimums_route_independently(
        self, tmp_path: Path, webhook_server: Any
    ) -> None:
        """warning event: critical-only endpoint is skipped (not even attempted),
        info endpoint receives — proven via the audit trail."""
        audit_path = tmp_path / "a.jsonl"
        router = AlertRouter(
            _config(
                _endpoint(
                    "http://127.0.0.1:9/nope",
                    endpoint_id="webhook-1",
                    min_severity=EventSeverity.CRITICAL,
                ),
                _endpoint(
                    webhook_server.url,
                    endpoint_id="webhook-2",
                    min_severity=EventSeverity.INFO,
                ),
                audit_path=audit_path,
            )
        )
        router.route(_ops_event(severity=EventSeverity.WARNING))
        assert len(webhook_server.received) == 1
        entries = AuditLog(audit_path).query()
        assert [e.details["endpoint_id"] for e in entries] == ["webhook-2"]

    def test_event_type_allowlist_respected(
        self, tmp_path: Path, webhook_server: Any
    ) -> None:
        router = AlertRouter(
            _config(
                _endpoint(
                    webhook_server.url,
                    event_types=frozenset({"ops.backup.failed"}),
                ),
                audit_path=tmp_path / "a.jsonl",
            )
        )
        router.route(_ops_event())  # ops.quota.breached — filtered out
        assert webhook_server.received == []

    def test_non_ops_events_ignored(
        self, tmp_path: Path, webhook_server: Any
    ) -> None:
        router = AlertRouter(
            _config(_endpoint(webhook_server.url), audit_path=tmp_path / "a.jsonl")
        )
        router.route(
            LifecycleEvent(
                type=EventType.CAPSULE_CREATED,
                subject=Subject(kind=SubjectKind.CAPSULE, ref="run-abc"),
            )
        )
        assert webhook_server.received == []


# --------------------------------------------------------------------------- #
# 6. Fail-safe delivery with bounded retries
# --------------------------------------------------------------------------- #


class TestFailSafeDelivery:
    def test_http_500_bounded_retries_observed(
        self, tmp_path: Path, webhook_server: Any
    ) -> None:
        webhook_server.server.response_code = 500
        audit_path = tmp_path / "a.jsonl"
        router = AlertRouter(
            _config(
                _endpoint(webhook_server.url),
                audit_path=audit_path,
                max_retries=1,
            )
        )
        router.route(_ops_event())  # must not raise
        assert len(webhook_server.received) == 2  # 1 attempt + 1 bounded retry
        entries = AuditLog(audit_path).query()
        assert len(entries) == 1
        assert entries[0].details["outcome"] == "error"
        assert entries[0].details["attempts"] == 2

    def test_connection_refused_never_raises(self, tmp_path: Path) -> None:
        audit_path = tmp_path / "a.jsonl"
        router = AlertRouter(
            _config(_endpoint("http://127.0.0.1:9/nope"), audit_path=audit_path)
        )
        router.route(_ops_event())  # must not raise
        entries = AuditLog(audit_path).query()
        assert len(entries) == 1
        assert entries[0].details["outcome"] == "error"


# --------------------------------------------------------------------------- #
# 7. Signing (rides the ADR-0137 path)
# --------------------------------------------------------------------------- #


class TestSigning:
    def test_delivery_signed_and_verifiable(
        self, tmp_path: Path, webhook_server: Any
    ) -> None:
        router = AlertRouter(
            _config(
                _endpoint(webhook_server.url),
                audit_path=tmp_path / "a.jsonl",
                sign_secret=b"s3cret",
                sign_keyid="ops-1",
            )
        )
        router.route(_ops_event())
        assert len(webhook_server.received) == 1
        record = json.loads(webhook_server.received[0]["body"])
        assert record["signature"]["alg"] == "hmac-sha256"
        assert record["signature"]["keyid"] == "ops-1"
        assert verify_record(record, b"s3cret") is True
        assert verify_record(record, b"wrong") is False
        headers = webhook_server.received[0]["headers"]
        assert headers.get("X-NovaFabric-Signature") == record["signature"]["value"]


# --------------------------------------------------------------------------- #
# 8. Hygiene (reuses events/hygiene.py)
# --------------------------------------------------------------------------- #


class TestHygiene:
    def test_seeded_secret_never_leaves(
        self, tmp_path: Path, webhook_server: Any
    ) -> None:
        router = AlertRouter(
            _config(_endpoint(webhook_server.url), audit_path=tmp_path / "a.jsonl")
        )
        router.route(
            _ops_event(payload={"note": "context sk-ant-" + "a1B2" * 8 + " leaked"})
        )
        body = webhook_server.received[0]["body"]
        assert b"sk-ant-" not in body
        assert b"[REDACTED:anthropic-api-key]" in body


# --------------------------------------------------------------------------- #
# 9. Audit trail (hash-chained, one entry per attempted delivery)
# --------------------------------------------------------------------------- #


class TestAuditTrail:
    def test_one_entry_per_attempted_delivery_chain_intact(
        self, tmp_path: Path, webhook_server: Any
    ) -> None:
        audit_path = tmp_path / "a.jsonl"
        router = AlertRouter(
            _config(
                _endpoint(webhook_server.url, endpoint_id="webhook-1"),
                _endpoint(webhook_server.url, endpoint_id="webhook-2"),
                audit_path=audit_path,
            )
        )
        event = _ops_event()
        router.route(event)
        assert len(webhook_server.received) == 2
        log = AuditLog(audit_path)
        entries: list[AuditEntry] = log.query()
        assert len(entries) == 2
        assert {e.details["endpoint_id"] for e in entries} == {
            "webhook-1",
            "webhook-2",
        }
        for entry in entries:
            assert entry.event_type.value == "alert.delivery"
            assert entry.resource_id == event.event_id
            assert entry.details["event_id"] == event.event_id
            assert entry.details["event_type"] == "ops.quota.breached"
            assert entry.details["outcome"] == "delivered"
            assert entry.details["attempts"] == 1
        assert log.verify() == []  # hash chain intact

    def test_audit_failure_swallowed_delivery_still_happens(
        self, tmp_path: Path, webhook_server: Any
    ) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("a file, not a directory")
        router = AlertRouter(
            _config(
                _endpoint(webhook_server.url),
                audit_path=blocker / "audit.jsonl",  # unwritable path
            )
        )
        router.route(_ops_event())  # must not raise
        assert len(webhook_server.received) == 1


# --------------------------------------------------------------------------- #
# 10. Wired source: ops.quota.breached from the ADR-0179 hard rejection path
# --------------------------------------------------------------------------- #


class TestQuotaBreachWiring:
    def _checker(self, **kwargs: Any) -> Any:
        pytest.importorskip("fastapi")
        from novafabric.server.config import QuotaConfig
        from novafabric.server.quotas import QuotaChecker, QuotaUsage

        return QuotaChecker(
            QuotaConfig(max_capsules_hard=1),
            lambda: QuotaUsage(capsules=1, total_bytes=0),
            audit_hook=lambda payload: None,  # keep the house serve-audit out
            **kwargs,
        )

    def test_hard_breach_emits_ops_alert(
        self, tmp_path: Path, webhook_server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_ALERTS_WEBHOOK", webhook_server.url)
        monkeypatch.setenv("NOVA_ALERTS_MIN_SEVERITY", "info")
        monkeypatch.setenv("NOVA_ALERTS_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
        checker = self._checker()
        decision = checker.check()
        assert decision.outcome == "reject"
        assert _wait_for(lambda: len(webhook_server.received) >= 1)
        record = json.loads(webhook_server.received[0]["body"])
        assert record["type"] == "ops.quota.breached"
        assert record["severity"] == "critical"
        assert record["subject"] == {"kind": "ops", "ref": "quota:capsules", "digest": None}
        assert record["payload"] == {"kind": "capsules", "usage": 1, "limit": 1}
        assert _wait_for(lambda: (tmp_path / "audit.jsonl").exists())
        entries = AuditLog(tmp_path / "audit.jsonl").query()
        assert len(entries) == 1
        assert entries[0].details["outcome"] == "delivered"

    def test_endpoint_down_quota_decision_unchanged_and_fast(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_ALERTS_WEBHOOK", "http://127.0.0.1:9/nope")
        monkeypatch.setenv("NOVA_ALERTS_MIN_SEVERITY", "info")
        monkeypatch.setenv("NOVA_ALERTS_TIMEOUT_S", "0.2")
        monkeypatch.setenv("NOVA_ALERTS_MAX_RETRIES", "0")
        monkeypatch.setenv("NOVA_ALERTS_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
        checker = self._checker()
        start = time.monotonic()
        decision = checker.check()  # must not raise
        elapsed = time.monotonic() - start
        assert decision.outcome == "reject"
        assert decision.hard is not None
        assert elapsed < 1.0  # delivery is off the request path

    def test_no_alerts_config_means_no_emission(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = tmp_path / "events.jsonl"
        monkeypatch.setenv("NOVA_EVENTS_LOG", str(log))
        checker = self._checker()
        decision = checker.check()
        assert decision.outcome == "reject"
        time.sleep(0.1)
        assert not log.exists()  # byte-identical to pre-ADR-0192 behavior
        assert not (tmp_path / "audit.jsonl").exists()

    def test_soft_breach_emits_no_alert(
        self, tmp_path: Path, webhook_server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("fastapi")
        from novafabric.server.config import QuotaConfig
        from novafabric.server.quotas import QuotaChecker, QuotaUsage

        monkeypatch.setenv("NOVA_ALERTS_WEBHOOK", webhook_server.url)
        monkeypatch.setenv("NOVA_ALERTS_MIN_SEVERITY", "info")
        monkeypatch.setenv("NOVA_ALERTS_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
        checker = QuotaChecker(
            QuotaConfig(max_capsules_soft=1, max_capsules_hard=5),
            lambda: QuotaUsage(capsules=1, total_bytes=0),
            audit_hook=lambda payload: None,
        )
        decision = checker.check()
        assert decision.outcome == "warn"
        time.sleep(0.2)
        assert webhook_server.received == []
