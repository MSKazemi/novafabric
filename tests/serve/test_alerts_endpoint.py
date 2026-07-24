"""ADR-0192 slice 2: the `GET /api/alerts/recent` dashboard read endpoint.

Merges the hash-chained delivery audit log (`alert.delivery` entries carry
endpoint_id/outcome/attempts) with recent `ops.*` events from the durable
`events.jsonl` (emitted-but-maybe-undelivered ⇒ outcome ``emitted``), sorted
newest-first and capped at ``limit``. Bounded read; never 500s on missing
files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novafabric.audit import AuditEventType, AuditLog
from novafabric.events.model import (
    EventSeverity,
    EventType,
    LifecycleEvent,
    Subject,
    SubjectKind,
)
from novafabric.serve.app import create_app

TOKEN = "testtoken"
H = {"host": "127.0.0.1:4321"}

_ALERTS_ENV = [
    "NOVA_ALERTS_WEBHOOK",
    "NOVA_ALERTS_AUDIT_LOG",
    "NOVA_ALERTS_MIN_SEVERITY",
    "NOVA_EVENTS_LOG",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for var in _ALERTS_ENV:
        monkeypatch.delenv(var, raising=False)
    # Point the audit log at a per-test path so the endpoint never reads the
    # developer's real house audit log.
    monkeypatch.setenv("NOVA_ALERTS_AUDIT_LOG", str(tmp_path / "audit.jsonl"))


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    base = tmp_path / "capsules"
    base.mkdir()
    app = create_app(
        token=TOKEN, capsule_dir=base, db_path=tmp_path / "r.db", static_dir=None
    )
    return TestClient(app)


def _seed_delivery(
    audit_path: Path,
    *,
    event_id: str,
    endpoint_id: str = "webhook-1",
    event_type: str = "ops.quota.breached",
    outcome: str = "delivered",
    attempts: int = 1,
    severity: str | None = "critical",
    subject: str = "quota:capsules",
) -> None:
    log = AuditLog(audit_path)
    log.append(
        event_type=AuditEventType.ALERT_DELIVERY,
        actor="alert-router",
        resource_id=event_id,
        details={
            "endpoint_id": endpoint_id,
            "event_id": event_id,
            "event_type": event_type,
            "outcome": outcome,
            "attempts": attempts,
            "severity": severity,
            "subject": subject,
        },
    )


def _write_events(events_path: Path, events: list[LifecycleEvent]) -> None:
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("a", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev.to_record()) + "\n")


def _ops_event(
    *,
    event_id: str,
    occurred_at: str,
    event_type: EventType = EventType.OPS_QUOTA_BREACHED,
    ref: str = "quota:capsules",
) -> LifecycleEvent:
    return LifecycleEvent(
        event_id=event_id,
        type=event_type,
        severity=EventSeverity.CRITICAL,
        subject=Subject(kind=SubjectKind.OPS, ref=ref),
        occurred_at=occurred_at,
        payload={"kind": "capsules"},
        source="nova server",
    )


# --------------------------------------------------------------------------- #


def test_requires_token(client: TestClient) -> None:
    r = client.get("/api/alerts/recent", headers=H)
    assert r.status_code == 401


def test_empty_returns_empty_shape(client: TestClient) -> None:
    r = client.get("/api/alerts/recent", params={"token": TOKEN}, headers=H)
    assert r.status_code == 200
    data = r.json()
    assert data == {"alerts": [], "total": 0, "alerting_configured": False}


def test_delivery_audit_entry_surfaces(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    _seed_delivery(audit_path, event_id="EVT1", endpoint_id="webhook-2", attempts=3)
    monkeypatch.setenv("NOVA_ALERTS_WEBHOOK", "http://a.internal/h")
    r = client.get("/api/alerts/recent", params={"token": TOKEN}, headers=H)
    data = r.json()
    assert data["alerting_configured"] is True
    assert data["total"] == 1
    alert = data["alerts"][0]
    assert alert["event_type"] == "ops.quota.breached"
    assert alert["outcome"] == "delivered"
    assert alert["endpoint_id"] == "webhook-2"
    assert alert["attempts"] == 3
    assert alert["severity"] == "critical"
    assert alert["subject"] == "quota:capsules"
    assert alert["id"]
    assert alert["timestamp"]


def test_error_outcome_maps_to_failed(
    client: TestClient, tmp_path: Path
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    _seed_delivery(audit_path, event_id="EVT-ERR", outcome="error", attempts=2)
    r = client.get("/api/alerts/recent", params={"token": TOKEN}, headers=H)
    alert = r.json()["alerts"][0]
    assert alert["outcome"] == "failed"
    assert alert["attempts"] == 2


def test_emitted_events_surface(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(
        events_path,
        [_ops_event(event_id="E-EMIT", occurred_at="2026-07-17T10:00:00.000000Z")],
    )
    monkeypatch.setenv("NOVA_EVENTS_LOG", str(events_path))
    r = client.get("/api/alerts/recent", params={"token": TOKEN}, headers=H)
    data = r.json()
    assert data["total"] == 1
    alert = data["alerts"][0]
    assert alert["id"] == "E-EMIT"
    assert alert["outcome"] == "emitted"
    assert alert["endpoint_id"] is None
    assert alert["attempts"] == 0
    assert alert["event_type"] == "ops.quota.breached"
    assert alert["severity"] == "critical"


def test_non_ops_events_in_log_are_ignored(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(
        events_path,
        [
            LifecycleEvent(
                event_id="CAP1",
                type=EventType.CAPSULE_CREATED,
                subject=Subject(kind=SubjectKind.CAPSULE, ref="run-1"),
                occurred_at="2026-07-17T09:00:00.000000Z",
            )
        ],
    )
    monkeypatch.setenv("NOVA_EVENTS_LOG", str(events_path))
    r = client.get("/api/alerts/recent", params={"token": TOKEN}, headers=H)
    assert r.json() == {"alerts": [], "total": 0, "alerting_configured": False}


def test_delivered_event_not_double_counted(
    client: TestClient, tmp_path: Path
) -> None:
    """An event that was both emitted and delivered appears once — as the
    delivery row, not a second 'emitted' row."""
    audit_path = tmp_path / "audit.jsonl"
    events_path = tmp_path / "events.jsonl"
    _seed_delivery(audit_path, event_id="SHARED", outcome="delivered")
    _write_events(
        events_path,
        [_ops_event(event_id="SHARED", occurred_at="2026-07-17T08:00:00.000000Z")],
    )
    import os

    os.environ["NOVA_EVENTS_LOG"] = str(events_path)
    try:
        r = client.get("/api/alerts/recent", params={"token": TOKEN}, headers=H)
    finally:
        os.environ.pop("NOVA_EVENTS_LOG", None)
    data = r.json()
    assert data["total"] == 1
    assert data["alerts"][0]["outcome"] == "delivered"


def test_merged_sorted_desc_and_limit(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events_path = tmp_path / "events.jsonl"
    _write_events(
        events_path,
        [
            _ops_event(event_id="OLD", occurred_at="2026-07-17T08:00:00.000000Z"),
            _ops_event(event_id="MID", occurred_at="2026-07-17T10:00:00.000000Z"),
            _ops_event(event_id="NEW", occurred_at="2026-07-17T12:00:00.000000Z"),
        ],
    )
    monkeypatch.setenv("NOVA_EVENTS_LOG", str(events_path))
    r = client.get(
        "/api/alerts/recent", params={"token": TOKEN, "limit": 2}, headers=H
    )
    data = r.json()
    assert data["total"] == 3  # full merged count
    assert [a["id"] for a in data["alerts"]] == ["NEW", "MID"]  # desc, capped at 2


def test_limit_bounds_are_enforced(client: TestClient) -> None:
    assert client.get(
        "/api/alerts/recent", params={"token": TOKEN, "limit": 0}, headers=H
    ).status_code == 422
    assert client.get(
        "/api/alerts/recent", params={"token": TOKEN, "limit": 201}, headers=H
    ).status_code == 422


def test_malformed_lines_skipped_not_fatal(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt line in either log skips that line, not the whole result
    (ADR-0199 bounded tail reads parse line-by-line)."""
    audit_path = tmp_path / "audit.jsonl"
    _seed_delivery(audit_path, event_id="GOOD")
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write("{corrupt audit line\n")
    events_path = tmp_path / "events.jsonl"
    _write_events(
        events_path,
        [_ops_event(event_id="E-OK", occurred_at="2026-07-17T11:00:00.000000Z")],
    )
    with events_path.open("a", encoding="utf-8") as fh:
        fh.write("not json either\n")
    monkeypatch.setenv("NOVA_EVENTS_LOG", str(events_path))
    r = client.get("/api/alerts/recent", params={"token": TOKEN}, headers=H)
    data = r.json()
    assert data["total"] == 2
    assert {a["id"] for a in data["alerts"]} == {"E-OK"} | {
        a["id"] for a in data["alerts"] if a["outcome"] == "delivered"
    }


def test_large_events_log_stays_bounded_and_returns_newest(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With many more events than 2×limit, the newest ones still surface."""
    events_path = tmp_path / "events.jsonl"
    _write_events(
        events_path,
        [
            _ops_event(
                event_id=f"E{i:04d}",
                occurred_at=f"2026-07-17T10:{i // 60:02d}:{i % 60:02d}.000000Z",
            )
            for i in range(500)
        ],
    )
    monkeypatch.setenv("NOVA_EVENTS_LOG", str(events_path))
    r = client.get(
        "/api/alerts/recent", params={"token": TOKEN, "limit": 5}, headers=H
    )
    data = r.json()
    assert [a["id"] for a in data["alerts"]] == [
        "E0499", "E0498", "E0497", "E0496", "E0495"
    ]
    assert data["total"] == 10  # bounded scan window (2×limit), not the full file


def test_missing_files_never_500(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_ALERTS_AUDIT_LOG", str(tmp_path / "nope" / "audit.jsonl"))
    monkeypatch.setenv("NOVA_EVENTS_LOG", str(tmp_path / "nope" / "events.jsonl"))
    r = client.get("/api/alerts/recent", params={"token": TOKEN}, headers=H)
    assert r.status_code == 200
    assert r.json()["total"] == 0
