"""DSAR-SLA turnaround computation over recorded timestamps (ADR-0161 D7 / NF-298).

Computes the turnaround of a subject-rights request — from a controller-supplied request-open
timestamp to the timestamp the request was fulfilled (the DSAR assembly / erasure) — against a
configured deadline (default GDPR Art. 12(3), one month). It is a **computation over recorded
timestamps**, not a workflow clock or a ticketing system: evidence that a request was met on time,
not the mechanism that meets it.

``met_deadline`` is a **factual** ``fulfilled_at <= deadline`` comparison — the same shape as the
incident clock's ``overdue`` field — deliberately named to avoid implying a compliance verdict.
Whether the recorded turnaround satisfies a controller's legal obligation is a determination for the
controller; there is no within_sla/compliant/verdict field. The record is keyed on the DSAR HMAC
pseudonym, never a raw subject id.

This first slice is the pure computation over supplied timestamps; sealing it into a signed proof
via the existing evidence-bundle path, and sourcing ``fulfilled_at`` from the shipped
``assemble_dsar`` package, are documented follow-ons.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel

#: GDPR Art. 12(3) response window — one month, approximated as 30 days.
DEFAULT_SLA_DAYS = 30
_DEFAULT_BASIS = "GDPR Art. 12(3) — respond without undue delay and within one month (30d)"


class DSARSLARecord(BaseModel):
    subject_hmac: str | None = None  # DSAR HMAC pseudonym — never a raw subject id
    request_open: datetime
    fulfilled_at: datetime
    deadline: datetime
    turnaround_seconds: float
    met_deadline: bool  # factual fulfilled_at <= deadline — NOT a compliance verdict
    basis: str
    # Intentionally NO within_sla/compliant/verdict/lawful field beyond the factual met_deadline.


def _require_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware (UTC)")
    return value


def compute_dsar_sla(
    *,
    request_open: datetime,
    fulfilled_at: datetime,
    deadline: datetime | None = None,
    subject_hmac: str | None = None,
    basis: str = _DEFAULT_BASIS,
) -> DSARSLARecord:
    """Compute the turnaround of a DSAR from ``request_open`` to ``fulfilled_at``.

    Both timestamps must be timezone-aware; ``fulfilled_at`` may not precede ``request_open``. The
    ``deadline`` defaults to ``request_open`` + one month (30 days). ``met_deadline`` is the factual
    ``fulfilled_at <= deadline`` — a time comparison, not a compliance determination.
    """
    request_open = _require_utc(request_open, "request_open")
    fulfilled_at = _require_utc(fulfilled_at, "fulfilled_at")
    if fulfilled_at < request_open:
        raise ValueError("fulfilled_at cannot be earlier than request_open")
    if deadline is None:
        deadline = request_open + timedelta(days=DEFAULT_SLA_DAYS)
    else:
        deadline = _require_utc(deadline, "deadline")

    turnaround = (fulfilled_at - request_open).total_seconds()
    return DSARSLARecord(
        subject_hmac=subject_hmac,
        request_open=request_open,
        fulfilled_at=fulfilled_at,
        deadline=deadline,
        turnaround_seconds=turnaround,
        met_deadline=fulfilled_at <= deadline,
        basis=basis,
    )
