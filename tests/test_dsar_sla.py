"""Tests for DSAR-SLA turnaround computation (ADR-0161 D7 / NF-298)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from novafabric.compliance.governance.dsar_sla import (
    DSARSLARecord,
    compute_dsar_sla,
)

_OPEN = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def test_default_deadline_is_one_month() -> None:
    rec = compute_dsar_sla(request_open=_OPEN, fulfilled_at=_OPEN + timedelta(days=5))
    assert isinstance(rec, DSARSLARecord)
    assert rec.deadline == _OPEN + timedelta(days=30)


def test_met_deadline_true_when_fulfilled_before_deadline() -> None:
    rec = compute_dsar_sla(request_open=_OPEN, fulfilled_at=_OPEN + timedelta(days=10))
    assert rec.met_deadline is True
    assert rec.turnaround_seconds == pytest.approx(timedelta(days=10).total_seconds())


def test_met_deadline_false_when_fulfilled_after_deadline() -> None:
    rec = compute_dsar_sla(request_open=_OPEN, fulfilled_at=_OPEN + timedelta(days=40))
    assert rec.met_deadline is False


def test_boundary_equal_to_deadline_is_met() -> None:
    rec = compute_dsar_sla(
        request_open=_OPEN,
        fulfilled_at=_OPEN + timedelta(days=7),
        deadline=_OPEN + timedelta(days=7),
    )
    assert rec.met_deadline is True


def test_explicit_deadline_overrides_default() -> None:
    rec = compute_dsar_sla(
        request_open=_OPEN,
        fulfilled_at=_OPEN + timedelta(days=3),
        deadline=_OPEN + timedelta(days=2),
    )
    assert rec.deadline == _OPEN + timedelta(days=2)
    assert rec.met_deadline is False


def test_subject_hmac_carried_no_raw_id() -> None:
    rec = compute_dsar_sla(
        request_open=_OPEN, fulfilled_at=_OPEN + timedelta(days=1), subject_hmac="hmac:abc",
    )
    assert rec.subject_hmac == "hmac:abc"
    # No raw-subject-id field on the record.
    assert "subject_id" not in rec.model_dump()


def test_no_verdict_field_beyond_met_deadline() -> None:
    rec = compute_dsar_sla(request_open=_OPEN, fulfilled_at=_OPEN + timedelta(days=1))
    forbidden = {"within_sla", "compliant", "verdict", "pass", "passed", "lawful"}
    assert forbidden.isdisjoint(rec.model_dump().keys())


def test_fulfilled_before_open_rejected() -> None:
    with pytest.raises(ValueError):
        compute_dsar_sla(request_open=_OPEN, fulfilled_at=_OPEN - timedelta(days=1))


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError):
        compute_dsar_sla(
            request_open=datetime(2026, 6, 1, 12, 0),  # noqa: DTZ001 — intentionally naive
            fulfilled_at=_OPEN + timedelta(days=1),
        )
