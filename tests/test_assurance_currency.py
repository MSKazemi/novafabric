"""ADR-0166 D2 — assurance-case currency ledger (continuous certification).

Per node: a validity window and last-refreshed timestamp yield an ``interval_status``
(current | due | overdue), computed **offline from a supplied sealed timestamp** — never
a system/network clock. An overdue node produces a ``drift_record`` flagged ``stale``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from novafabric.assure.currency import (
    CurrencyLedger,
    DriftReason,
    IntervalStatus,
    NodeCurrency,
    compute_interval_status,
    drift_records,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _node(**kw) -> NodeCurrency:
    base = {
        "node_id": "Sn1",
        "last_refreshed": T0,
        "evidence_window": timedelta(days=30),
        "due_lead": timedelta(days=5),
    }
    base.update(kw)
    return NodeCurrency(**base)


def test_current_when_well_within_the_window():
    status = compute_interval_status(_node(), as_of=T0 + timedelta(days=10))
    assert status is IntervalStatus.current


def test_due_when_inside_the_due_lead_before_expiry():
    # expiry = T0+30d; due window starts at T0+25d.
    status = compute_interval_status(_node(), as_of=T0 + timedelta(days=27))
    assert status is IntervalStatus.due


def test_overdue_at_and_after_expiry():
    assert compute_interval_status(_node(), as_of=T0 + timedelta(days=30)) is IntervalStatus.overdue
    assert compute_interval_status(_node(), as_of=T0 + timedelta(days=99)) is IntervalStatus.overdue


def test_status_is_deterministic_in_as_of_no_hidden_clock():
    # Same node + same as_of always yields the same status — the function reads no
    # ambient clock; the caller supplies the (sealed) reference time.
    node = _node()
    a = compute_interval_status(node, as_of=T0 + timedelta(days=27))
    b = compute_interval_status(node, as_of=T0 + timedelta(days=27))
    assert a is b is IntervalStatus.due


def test_ledger_reports_status_per_node():
    ledger = CurrencyLedger(
        nodes=[
            _node(node_id="A", last_refreshed=T0, evidence_window=timedelta(days=30)),
            _node(node_id="B", last_refreshed=T0, evidence_window=timedelta(days=5)),
        ]
    )
    statuses = ledger.statuses(as_of=T0 + timedelta(days=10))
    assert statuses == {"A": IntervalStatus.current, "B": IntervalStatus.overdue}


def test_overdue_node_yields_a_drift_record():
    ledger = CurrencyLedger(nodes=[_node(node_id="B", evidence_window=timedelta(days=5))])
    records = drift_records(ledger, as_of=T0 + timedelta(days=10))
    assert len(records) == 1
    rec = records[0]
    assert rec.node_id == "B"
    assert rec.status == "stale"
    assert rec.reason is DriftReason.evidence_expired


def test_no_drift_records_when_everything_is_current():
    ledger = CurrencyLedger(nodes=[_node(node_id="A", evidence_window=timedelta(days=30))])
    assert drift_records(ledger, as_of=T0 + timedelta(days=1)) == []
