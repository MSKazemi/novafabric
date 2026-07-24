"""R4 (ADR-0199..0201) — four enterprise report templates.

Covers, per builder: populated store + empty store (graceful empty rows,
never an exception). Plus the registry-mounted data routes: token guard,
{columns, rows, count} JSON shape, CSV round-trip with Content-Disposition,
required-filter 422 for compliance-posture, catalog listing, and HTML export
(including the derived per-day chart, which is export-only by design).

Fixtures mirror tests/serve/test_alerts_endpoint.py (audit/events files),
test_audit_tail_pagination.py (dashboard audit log), and
test_admin_keys_endpoint.py (api-key store).
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from novafabric.audit import AuditEventType, AuditLog
from novafabric.events.model import (
    EventSeverity,
    EventType,
    LifecycleEvent,
    Subject,
    SubjectKind,
)
from novafabric.serve import audit as serve_audit
from novafabric.serve import reports as _reports
from novafabric.serve.app import create_app
from novafabric.serve.report_registry import REPORTS
from novafabric.server.api_keys import create_key, parse_key, revoke_key

TOKEN = "testtoken"
H = {"host": "127.0.0.1:4321"}

_ALERTS_ENV = [
    "NOVA_ALERTS_WEBHOOK",
    "NOVA_ALERTS_AUDIT_LOG",
    "NOVA_ALERTS_MIN_SEVERITY",
    "NOVA_EVENTS_LOG",
]

R4_IDS = ("alert-digest", "api-key-inventory", "dashboard-audit", "compliance-posture")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Per-test paths so builders never touch the developer's real logs."""
    for var in _ALERTS_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NOVA_ALERTS_AUDIT_LOG", str(tmp_path / "alerts-audit.jsonl"))
    monkeypatch.setenv(serve_audit.AUDIT_ENV, str(tmp_path / "dashboard-audit.jsonl"))


# ── seeding helpers (mirroring the sibling test modules) ─────────────────────


def _seed_delivery(
    audit_path: Path,
    *,
    event_id: str,
    outcome: str = "delivered",
    endpoint_id: str = "webhook-1",
    attempts: int = 1,
) -> None:
    AuditLog(audit_path).append(
        event_type=AuditEventType.ALERT_DELIVERY,
        actor="alert-router",
        resource_id=event_id,
        details={
            "endpoint_id": endpoint_id,
            "event_id": event_id,
            "event_type": "ops.quota.breached",
            "outcome": outcome,
            "attempts": attempts,
            "severity": "critical",
            "subject": "quota:capsules",
        },
    )


def _write_ops_event(events_path: Path, event_id: str, occurred_at: str) -> None:
    ev = LifecycleEvent(
        event_id=event_id,
        type=EventType.OPS_QUOTA_BREACHED,
        severity=EventSeverity.CRITICAL,
        subject=Subject(kind=SubjectKind.OPS, ref="quota:capsules"),
        occurred_at=occurred_at,
        payload={"kind": "capsules"},
        source="nova server",
    )
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev.to_record()) + "\n")


def _write_capsule(capsule_dir: Path, run_id: str) -> Path:
    """Minimal valid capsule (mirrors tests/compliance/export/conftest.py)."""
    d = capsule_dir / run_id
    d.mkdir(parents=True)
    for name in ("model-calls.jsonl", "tool-calls.jsonl", "trace.jsonl", "assets.jsonl"):
        (d / name).touch()
    manifest = {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "created_at": "2026-07-01T00:00:00.000000Z",
        "finished_at": "2026-07-01T00:00:10.000000Z",
        "duration_ms": 10000,
        "status": "success",
        "command": ["python", "agent.py"],
        "capture_mode": "cli-wrapper",
        "novafabric_version": "0.63.0",
        "working_directory": "~/projects/agent",
        "host": {"os": "linux", "arch": "x86_64", "python": "3.12.0"},
        "environment_ref": "env.lock",
        "replay_policy_ref": "replay.yaml",
        "trace_ref": "trace.jsonl",
        "model_calls_ref": "model-calls.jsonl",
        "tool_calls_ref": "tool-calls.jsonl",
        "assets_ref": "assets.jsonl",
        "model_call_count": 5,
        "tool_call_count": 3,
        "exit_code": 0,
    }
    (d / "capsule.yaml").write_text(yaml.dump(manifest))
    return d


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    base = tmp_path / "capsules"
    base.mkdir(exist_ok=True)
    app = create_app(
        token=TOKEN, capsule_dir=base, db_path=tmp_path / "registry.db", static_dir=None
    )
    return TestClient(app)


# ── 1. alert-digest builder ──────────────────────────────────────────────────


def test_alert_digest_builder_populated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit_path = tmp_path / "alerts-audit.jsonl"
    _seed_delivery(audit_path, event_id="D-OK", outcome="delivered")
    _seed_delivery(audit_path, event_id="D-ERR", outcome="error", attempts=2)
    events_path = tmp_path / "events.jsonl"
    _write_ops_event(events_path, "E-EMIT", "2026-07-17T10:00:00.000000Z")
    monkeypatch.setenv("NOVA_EVENTS_LOG", str(events_path))

    cols, rows = _reports.report_alert_digest()
    assert cols == _reports.ALERT_DIGEST_COLS
    by_detail = {r["detail"]: r for r in rows}
    kinds = {r["kind"] for r in rows}
    assert kinds == {"delivery", "emitted"}
    outcomes = {r["severity_or_outcome"] for r in rows if r["kind"] == "delivery"}
    assert outcomes == {"delivered", "failed"}  # "error" maps to "failed"
    emitted = by_detail["event_id=E-EMIT"]
    assert emitted["severity_or_outcome"] == "critical"  # severity for emitted
    assert emitted["endpoint_or_source"] == "quota:capsules"
    delivery = next(r for r in rows if r["kind"] == "delivery")
    assert delivery["endpoint_or_source"] == "webhook-1"
    assert delivery["event_type"] == "ops.quota.breached"
    assert all(r["ts"] for r in rows)


def test_alert_digest_builder_date_filter_and_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Empty: no files at all
    cols, rows = _reports.report_alert_digest()
    assert (cols, rows) == (_reports.ALERT_DIGEST_COLS, [])
    # Emitted-only, then filtered out by `to`
    events_path = tmp_path / "events.jsonl"
    _write_ops_event(events_path, "E1", "2026-07-17T10:00:00.000000Z")
    monkeypatch.setenv("NOVA_EVENTS_LOG", str(events_path))
    _, rows = _reports.report_alert_digest()
    assert len(rows) == 1
    _, rows = _reports.report_alert_digest(to_ts="2020-01-01")
    assert rows == []


# ── 2. api-key-inventory builder ─────────────────────────────────────────────


def test_api_key_inventory_builder_populated_no_secrets(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    key, record = create_key(
        "alice@x", ["reader", "writer"], actor="seed", db_path=db
    )
    _, secret = parse_key(key)  # type: ignore[misc]
    _, revoked = create_key("bob@x", ["reader"], actor="seed", db_path=db)
    revoke_key(revoked["key_id"], actor="seed", db_path=db)

    cols, rows = _reports.report_api_key_inventory(db)
    assert cols == _reports.API_KEY_INVENTORY_COLS
    by_id = {r["key_id"]: r for r in rows}
    alice = by_id[record["key_id"]]
    assert alice["name_or_owner"] == "alice@x"
    assert alice["roles"] == "reader,writer"
    assert alice["status"] == "active"
    assert by_id[revoked["key_id"]]["status"] == "revoked"
    dumped = json.dumps(rows)
    assert secret not in dumped
    assert "secret" not in dumped.lower()


def test_api_key_inventory_builder_empty(tmp_path: Path) -> None:
    assert _reports.report_api_key_inventory(None) == (
        _reports.API_KEY_INVENTORY_COLS, []
    )
    assert _reports.report_api_key_inventory(tmp_path / "missing.db") == (
        _reports.API_KEY_INVENTORY_COLS, []
    )


# ── 3. dashboard-audit builder ───────────────────────────────────────────────


def test_dashboard_audit_builder_populated_and_action_filter() -> None:
    for i in range(3):
        serve_audit.append(
            action="register_asset",
            args={"i": i},
            cli_equivalent=f"nova register x{i}",
            actor_token_fp="abcd1234",
        )
    serve_audit.append(
        action="delete_capsule",
        args={},
        cli_equivalent="nova capsule delete r1",
        actor_token_fp="abcd1234",
    )
    cols, rows = _reports.report_dashboard_audit()
    assert cols == _reports.DASHBOARD_AUDIT_COLS
    assert len(rows) == 4
    assert rows[0]["action"] == "delete_capsule"  # newest first
    assert rows[0]["cli_equivalent"] == "nova capsule delete r1"
    assert rows[0]["actor_token_fp"] == "abcd1234"
    assert rows[0]["result"] == "ok"
    _, filtered = _reports.report_dashboard_audit(action="register_asset")
    assert len(filtered) == 3
    assert {r["action"] for r in filtered} == {"register_asset"}


def test_dashboard_audit_builder_empty() -> None:
    assert _reports.report_dashboard_audit() == (_reports.DASHBOARD_AUDIT_COLS, [])


# ── 4. compliance-posture builder (headless: needs a run_id capsule) ─────────


def test_compliance_posture_builder_populated(tmp_path: Path) -> None:
    pytest.importorskip("novafabric.compliance.export.annex_iv")
    base = tmp_path / "capsules"
    _write_capsule(base, "01HTESTRUN0000000000000001")
    cols, rows = _reports.report_compliance_posture(
        base, "01HTESTRUN0000000000000001"
    )
    assert cols == _reports.COMPLIANCE_POSTURE_COLS
    assert len(rows) == 15  # the 15 mandatory Annex IV elements
    flags = {r["completeness_flag"] for r in rows}
    assert flags <= {"complete", "partial", "missing"}
    assert all(r["element_id"] for r in rows)
    assert all(r["title"] for r in rows)
    assert all(r["population_method"] for r in rows)


def test_compliance_posture_builder_empty_cases(tmp_path: Path) -> None:
    base = tmp_path / "capsules"
    base.mkdir()
    # unknown run
    assert _reports.report_compliance_posture(base, "nope") == (
        _reports.COMPLIANCE_POSTURE_COLS, []
    )
    # path-traversal-shaped run_id
    assert _reports.report_compliance_posture(base, "../etc") == (
        _reports.COMPLIANCE_POSTURE_COLS, []
    )
    # empty run_id (route layer 422s first; builder still degrades)
    assert _reports.report_compliance_posture(base, "") == (
        _reports.COMPLIANCE_POSTURE_COLS, []
    )


# ── routes: guard, shapes, CSV, catalog, export ──────────────────────────────


def test_data_route_requires_token(client: TestClient) -> None:
    assert client.get("/api/reports/alert-digest", headers=H).status_code == 401


def test_dashboard_audit_route_json_shape(client: TestClient) -> None:
    serve_audit.append(
        action="register_asset",
        args={},
        cli_equivalent="nova register a",
        actor_token_fp="ff00ff00",
    )
    r = client.get(
        "/api/reports/dashboard-audit", params={"token": TOKEN}, headers=H
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"columns", "rows", "count"}
    assert body["columns"] == _reports.DASHBOARD_AUDIT_COLS
    assert body["count"] == len(body["rows"]) == 1
    # action filter is whitelisted
    r2 = client.get(
        "/api/reports/dashboard-audit",
        params={"token": TOKEN, "action": "no-such-action"},
        headers=H,
    )
    assert r2.json()["count"] == 0


def test_api_key_inventory_route_csv_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    _, record = create_key("alice@x", ["reader"], actor="seed", db_path=db)
    base = tmp_path / "capsules"
    base.mkdir()
    app = create_app(token=TOKEN, capsule_dir=base, db_path=db, static_dir=None)
    client = TestClient(app)
    r = client.get(
        "/api/reports/api-key-inventory",
        params={"token": TOKEN, "format": "csv"},
        headers=H,
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert (
        r.headers["content-disposition"]
        == 'attachment; filename="api-key-inventory.csv"'
    )
    parsed = list(csv.reader(io.StringIO(r.text)))
    assert parsed[0] == _reports.API_KEY_INVENTORY_COLS
    row = dict(zip(parsed[0], parsed[1]))
    assert row["key_id"] == record["key_id"]
    assert row["name_or_owner"] == "alice@x"
    assert row["status"] == "active"
    assert "nvfk_" not in r.text  # never secret material


def test_compliance_posture_route_requires_run_id(client: TestClient) -> None:
    r = client.get(
        "/api/reports/compliance-posture", params={"token": TOKEN}, headers=H
    )
    assert r.status_code == 422
    assert "run_id" in r.json()["detail"]


def test_compliance_posture_route_populated(tmp_path: Path) -> None:
    pytest.importorskip("novafabric.compliance.export.annex_iv")
    base = tmp_path / "capsules"
    _write_capsule(base, "01HTESTRUN0000000000000002")
    app = create_app(
        token=TOKEN, capsule_dir=base, db_path=tmp_path / "r.db", static_dir=None
    )
    client = TestClient(app)
    r = client.get(
        "/api/reports/compliance-posture",
        params={"token": TOKEN, "run_id": "01HTESTRUN0000000000000002"},
        headers=H,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["columns"] == _reports.COMPLIANCE_POSTURE_COLS
    assert body["count"] == 15


def test_data_route_rejects_unknown_format(client: TestClient) -> None:
    r = client.get(
        "/api/reports/alert-digest",
        params={"token": TOKEN, "format": "docx"},
        headers=H,
    )
    assert r.status_code == 422


def test_catalog_includes_r4_reports(client: TestClient) -> None:
    r = client.get("/api/reports/catalog", params={"token": TOKEN}, headers=H)
    entries = {e["report_id"]: e for e in r.json()["reports"]}
    assert set(R4_IDS) <= set(entries)
    assert set(entries) == set(REPORTS)
    assert entries["compliance-posture"]["required_filters"] == ["run_id"]
    # Derived charts are export-only: the catalog honestly advertises no
    # preview chart (the web maps rows directly and cannot aggregate).
    for rid in R4_IDS:
        assert entries[rid]["chart"] is None


def test_alert_digest_html_export_has_derived_chart(
    client: TestClient, tmp_path: Path
) -> None:
    audit_path = tmp_path / "alerts-audit.jsonl"
    _seed_delivery(audit_path, event_id="OK1", outcome="delivered")
    _seed_delivery(audit_path, event_id="ERR1", outcome="error")
    r = client.get(
        "/api/reports/alert-digest/export",
        params={"token": TOKEN, "format": "html"},
        headers=H,
    )
    assert r.status_code == 200
    assert r.headers["content-disposition"].endswith('filename="alert-digest.html"')
    html = r.text
    assert "<svg" in html  # delivered-vs-failed per-day stack, derived server-side
    assert "delivered vs failed" in html


def test_compliance_posture_html_export_has_no_chart(tmp_path: Path) -> None:
    pytest.importorskip("novafabric.compliance.export.annex_iv")
    base = tmp_path / "capsules"
    _write_capsule(base, "01HTESTRUN0000000000000003")
    app = create_app(
        token=TOKEN, capsule_dir=base, db_path=tmp_path / "r.db", static_dir=None
    )
    client = TestClient(app)
    r = client.get(
        "/api/reports/compliance-posture/export",
        params={
            "token": TOKEN,
            "format": "html",
            "run_id": "01HTESTRUN0000000000000003",
        },
        headers=H,
    )
    assert r.status_code == 200
    assert "<svg" not in r.text  # chart deliberately omitted (honesty rule)
    assert "completeness_flag" in r.text
