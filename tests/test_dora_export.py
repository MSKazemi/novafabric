"""Tests for the EU DORA major-ICT-incident report projection (ADR-0159 D5 / NF-279)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from novafabric.compliance.incident.dora_export import (
    DoraIncidentReport,
    build_dora_report,
)
from novafabric.compliance.incident.models import Incident, IncidentSeverity


def _incident(aware_at: datetime | None = None) -> Incident:
    occurred = datetime(2026, 7, 10, 8, 0, tzinfo=timezone.utc)
    return Incident(
        id="inc-dora-1",
        title="ICT outage",
        classification="major_ict_incident",
        severity=IncidentSeverity.HIGH,
        occurred_at=occurred,
        aware_at=aware_at,
        run_ids=["run-1"],
    )


def test_completeness_entries_marked_operator_asserted() -> None:
    # ADR-0197 I-1: the DORA projection is a pure projection over the
    # operator-authored incident store; every field-group must say so.
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    report = build_dora_report(_incident(), now=now)
    assert report.completeness_summary
    for entry in report.completeness_summary:
        assert entry["evidence_source"] == "operator_asserted"


def test_builds_three_dora_stages() -> None:
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    report = build_dora_report(_incident(), now=now)
    assert isinstance(report, DoraIncidentReport)
    stages = [d.stage for d in report.deadlines]
    assert stages == ["initial_notification", "intermediate_report", "final_report"]


def test_deadlines_chain_from_anchor() -> None:
    aware = datetime(2026, 7, 10, 8, 0, tzinfo=timezone.utc)
    now = aware  # evaluate at the anchor
    report = build_dora_report(_incident(aware_at=aware), now=now)
    by_stage = {d.stage: d for d in report.deadlines}
    # initial: 24h from awareness; intermediate: +72h; final: +30d.
    assert by_stage["initial_notification"].deadline == aware + timedelta(hours=24)
    assert by_stage["intermediate_report"].deadline == aware + timedelta(hours=24 + 72)
    assert by_stage["final_report"].deadline == aware + timedelta(hours=24 + 72) + timedelta(days=30)


def test_overdue_flag_is_time_relative() -> None:
    aware = datetime(2026, 7, 10, 8, 0, tzinfo=timezone.utc)
    # 25h later: the 24h initial notification is overdue, the others are not.
    now = aware + timedelta(hours=25)
    report = build_dora_report(_incident(aware_at=aware), now=now)
    by_stage = {d.stage: d for d in report.deadlines}
    assert by_stage["initial_notification"].overdue is True
    assert by_stage["intermediate_report"].overdue is False


def test_anchor_falls_back_to_occurred_at() -> None:
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    report = build_dora_report(_incident(aware_at=None), now=now)
    occurred = datetime(2026, 7, 10, 8, 0, tzinfo=timezone.utc)
    initial = next(d for d in report.deadlines if d.stage == "initial_notification")
    assert initial.anchor == occurred


def test_transmitted_is_forced_false() -> None:
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    report = build_dora_report(_incident(), now=now)
    assert report.transmitted is False


def test_classification_timestamp_gap_is_recorded() -> None:
    # The 4h-from-classification bound needs a classified_at timestamp NovaFabric does
    # not store; that gap must be surfaced honestly, never silently fabricated.
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    report = build_dora_report(_incident(), now=now)
    missing = {e["field_name"] for e in report.completeness_summary}
    assert "classified_at" in missing


def test_no_verdict_or_compliance_field() -> None:
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    report = build_dora_report(_incident(), now=now)
    forbidden = {"compliant", "reported", "verdict", "on_time", "pass", "passed"}
    assert forbidden.isdisjoint(report.model_dump().keys())
