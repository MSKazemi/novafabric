"""Assurance-case currency ledger (ADR-0166 D2, first slice).

Continuous certification means an assurance case can go *stale*: a ``solution``
node's evidence has a validity window, and once that window closes the argument
is no longer current. This module computes, per node, an ``interval_status``
(``current | due | overdue``) from the node's ``last_refreshed`` timestamp and
``evidence_window`` — **offline, against a caller-supplied reference time**
(a sealed timestamp), never a system or network clock. Overdue nodes yield
``drift_record``s flagged ``stale``.

The caller passes ``as_of`` explicitly (from sealed evidence), so status is a pure
function of the ledger plus that time — reproducible and auditable.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum

from pydantic import BaseModel


class IntervalStatus(str, Enum):
    current = "current"  # comfortably inside the validity window
    due = "due"  # inside the due-lead before expiry — refresh soon
    overdue = "overdue"  # at or past expiry — no longer current


class DriftReason(str, Enum):
    evidence_expired = "evidence_expired"
    assumption_invalidated = "assumption_invalidated"
    bound_capsule_superseded = "bound_capsule_superseded"
    defeater_open = "defeater_open"
    standard_clause_revised = "standard_clause_revised"


class NodeCurrency(BaseModel):
    """Per-node validity window and last-refresh time."""

    node_id: str
    last_refreshed: datetime  # a sealed timestamp
    evidence_window: timedelta  # how long the evidence stays valid
    due_lead: timedelta = timedelta(0)  # how long before expiry to flag `due`


class DriftRecord(BaseModel):
    """A recorded staleness fact for one node — the argument drifted, not re-decided."""

    node_id: str
    status: str = "stale"
    reason: DriftReason
    triggered_by: str | None = None  # optional link to the superseding/expiring fact


def compute_interval_status(node: NodeCurrency, *, as_of: datetime) -> IntervalStatus:
    """Return the node's currency status at ``as_of`` (a supplied sealed time).

    ``as_of`` is required and never defaulted to the system clock — currency is
    computed only against sealed timestamps (ADR-0166 D2).
    """
    expiry = node.last_refreshed + node.evidence_window
    if as_of >= expiry:
        return IntervalStatus.overdue
    if as_of >= expiry - node.due_lead:
        return IntervalStatus.due
    return IntervalStatus.current


class CurrencyLedger(BaseModel):
    """The set of per-node currency entries for an assurance case."""

    nodes: list[NodeCurrency] = []

    def statuses(self, *, as_of: datetime) -> dict[str, IntervalStatus]:
        return {n.node_id: compute_interval_status(n, as_of=as_of) for n in self.nodes}


def drift_records(ledger: CurrencyLedger, *, as_of: datetime) -> list[DriftRecord]:
    """Emit a ``stale`` drift record for every overdue node at ``as_of``."""
    return [
        DriftRecord(node_id=n.node_id, reason=DriftReason.evidence_expired)
        for n in ledger.nodes
        if compute_interval_status(n, as_of=as_of) is IntervalStatus.overdue
    ]
