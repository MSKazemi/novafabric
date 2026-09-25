"""Financial-record retention-posture attestation (ADR-0159 D5 / NF-277).

A pure **renderer** over facts read from the shipped ADR-0031 retention engine and ADR-0030
trusted-timestamp output (gathered by :mod:`.retention_collect`). For a set of Evidence Bundles it
attests, each ``complete`` / ``partial`` / ``missing`` with facts or a machine-readable reason:

* registry posture — the ``retention-policy.yaml`` window/deletion mode and the legal-hold state;
* per bundle — the WORM lock (S3 Object Lock / Azure immutable / GCS Bucket Lock receipt, or the
  local dev/test adapter, which is reported as ``partial`` because it is not true WORM), the
  RFC 3161 trusted timestamp **as actually recorded in that bundle** (reported when present,
  ``missing``-with-reason only when one genuinely was not obtained — ADR-0159 correction of
  2026-07-20), and the hash-chained audit trail entries for the run.

It **attests posture, never compliance**: no field says a record set *is* 17a-4 or MiFID
compliant, and this module makes nothing immutable (the WORM adapter does that). Nothing is
fabricated.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from ..provenance import EvidenceSource, source_for_status
from .model_independence import FINANCE_HONESTY_BANNER

_RETENTION_GAP_STATES = frozenset({"missing"})

RetentionStatus = Literal["complete", "partial", "missing"]

#: RFC 3161 PKIStatus values that mean the TSA granted a token (RFC 3161 §2.4.2).
_PKI_GRANTED = frozenset({0, 1})


class Regime(str, Enum):
    """Record-retention regimes the attestation can be tagged with."""

    sec_17a_4 = "17a-4"
    mifid = "mifid"


#: Version-pinned regime text (ADR-0159 D1 — surface exactly which version is rendered).
REGIME_TEXT: dict[Regime, str] = {
    Regime.sec_17a_4: "17 CFR 240.17a-4 (2022 amendment; audit-trail alternative)",
    Regime.mifid: "MiFID II 2014/65/EU Art. 16(6) + Delegated Reg. (EU) 2017/565 Art. 72",
}

TSR_VERIFY_HINT = (
    "presence and manifest-digest binding checked; verify the TSA signature with "
    "`openssl ts -verify` (see the bundle README)"
)
LOCAL_WORM_REASON = "local SQLite WORM adapter is dev/test only — not true WORM (ADR-0031)"


# ---------------------------------------------------------------------------
# Input facts (produced by the collector; never guessed)
# ---------------------------------------------------------------------------


class PolicyFacts(BaseModel):
    """The ADR-0031 ``retention-policy.yaml`` fields as read from the registry."""

    model_config = ConfigDict(frozen=True)

    ref: str
    retention_days: int
    deletion_mode: str
    jurisdiction: str = ""
    legal_hold_ids: list[str] = []


class HoldFacts(BaseModel):
    """Legal-hold state of the registry from ``holds.jsonl`` (``present=False`` = file absent)."""

    model_config = ConfigDict(frozen=True)

    ref: str
    present: bool
    active_hold_ids: list[str] = []
    released_hold_ids: list[str] = []


class WormFacts(BaseModel):
    """A WORM lock for one run, from the local adapter or a supplied cloud WORM receipt."""

    model_config = ConfigDict(frozen=True)

    ref: str
    backend_type: str  # "s3" | "azure" | "gcs" | "local"
    locked_until: AwareDatetime
    confirmation: str | None = None
    integrity_ok: bool | None = None  # local adapter only: sha256 re-check


class TimestampFacts(BaseModel):
    """What one Evidence Bundle actually records about its RFC 3161 timestamp."""

    model_config = ConfigDict(frozen=True)

    manifest_status: str | None = None  # manifest.json ``timestamp_status`` ("ok" | "failed")
    failure_reason: str | None = None
    tsa_url: str | None = None
    manifest_tsr_sha256: str | None = None
    tsr_present: bool = False
    tsr_sha256: str | None = None
    pki_status: int | None = None
    pki_error: str | None = None


class AuditFacts(BaseModel):
    """Hash-chained audit-log entries for a run, plus the chain's integrity check."""

    model_config = ConfigDict(frozen=True)

    ref: str
    event_types: list[str] = []
    chain_errors: int = 0
    parse_error: str | None = None


class BundleFacts(BaseModel):
    """Everything read for one Evidence Bundle."""

    model_config = ConfigDict(frozen=True)

    bundle: str
    bundle_id: str | None = None
    run_id: str
    timestamp: TimestampFacts
    worm: WormFacts | None = None
    audit: AuditFacts | None = None
    audit_log_ref: str | None = None  # where the audit log was looked for, if it was absent


# ---------------------------------------------------------------------------
# Output artifact
# ---------------------------------------------------------------------------


class RetentionRow(BaseModel):
    """One attested element: status plus facts (present) or a reason (partial/missing)."""

    element: str
    status: RetentionStatus
    facts: dict[str, str] = {}
    source_refs: list[str] = []  # empty when missing — never fabricated
    reason: str | None = None
    evidence_source: EvidenceSource


class ArtifactRetention(BaseModel):
    """Per-bundle attestation rows."""

    bundle: str
    bundle_id: str | None = None
    run_id: str
    rows: list[RetentionRow]


class RetentionAttestation(BaseModel):
    """NF-277 retention-posture attestation. Intentionally NO compliant/verdict field."""

    regime: str
    banner: str
    as_of: str
    registry: str | None = None
    posture: list[RetentionRow]
    artifacts: list[ArtifactRetention]
    summary: dict[str, int]


def _row(
    element: str,
    status: RetentionStatus,
    *,
    facts: dict[str, str] | None = None,
    refs: Iterable[str] = (),
    reason: str | None = None,
) -> RetentionRow:
    return RetentionRow(
        element=element,
        status=status,
        facts=facts or {},
        source_refs=list(refs) if status != "missing" else [],
        reason=reason,
        evidence_source=source_for_status(status, gap_states=_RETENTION_GAP_STATES),
    )


def policy_row(registry: str | None, policy: PolicyFacts | None) -> RetentionRow:
    """Render the ``retention_policy`` posture row."""
    if registry is None:
        return _row("retention_policy", "missing", reason="no registry given; policy not read")
    if policy is None:
        return _row(
            "retention_policy",
            "missing",
            reason=f"no retention-policy.yaml for registry {registry!r}",
        )
    facts = {
        "retention_days": str(policy.retention_days),
        "deletion_mode": policy.deletion_mode,
        "jurisdiction": policy.jurisdiction,
        "legal_hold_ids": ",".join(policy.legal_hold_ids),
    }
    return _row("retention_policy", "complete", facts=facts, refs=[policy.ref])


def hold_row(registry: str | None, holds: HoldFacts | None) -> RetentionRow:
    """Render the ``legal_hold`` posture row (an absent hold file is an evidenced 'no hold')."""
    if registry is None or holds is None:
        return _row("legal_hold", "missing", reason="no registry given; legal-hold state not read")
    facts = {
        "active_hold_ids": ",".join(holds.active_hold_ids),
        "released_hold_ids": ",".join(holds.released_hold_ids),
        "hold_record": "present" if holds.present else "absent (no hold ever recorded)",
    }
    return _row("legal_hold", "complete", facts=facts, refs=[holds.ref])


def worm_row(run_id: str, worm: WormFacts | None, *, as_of: datetime) -> RetentionRow:
    """Render the per-run ``worm_lock`` row."""
    if worm is None:
        return _row(
            "worm_lock",
            "missing",
            reason=f"no WORM receipt found for run {run_id!r} (local worm.db or supplied receipt)",
        )
    facts = {
        "backend_type": worm.backend_type,
        "locked_until": worm.locked_until.isoformat(),
    }
    if worm.confirmation is not None:
        facts["confirmation"] = worm.confirmation
    reason: str | None = None
    if worm.integrity_ok is False:
        reason = "WORM integrity check failed (sha256 mismatch or not found)"
    elif worm.locked_until <= as_of:
        reason = f"WORM retention lock expired at {worm.locked_until.isoformat()}"
    elif worm.backend_type == "local":
        reason = LOCAL_WORM_REASON
    status: RetentionStatus = "complete" if reason is None else "partial"
    return _row("worm_lock", status, facts=facts, refs=[worm.ref], reason=reason)


def _digest(value: str | None) -> str | None:
    """Normalise a sha256 digest to bare lowercase hex (manifests write ``sha256:<hex>``)."""
    if value is None:
        return None
    return value.removeprefix("sha256:").lower()


def timestamp_row(bundle: str, ts: TimestampFacts) -> RetentionRow:
    """Render the per-bundle ``trusted_timestamp`` row from what the bundle actually records."""
    element = "trusted_timestamp"
    ref = f"{bundle}!manifest.dsse.tsr"
    if not ts.tsr_present:
        if ts.manifest_status == "failed":
            reason = f"timestamp request failed at export: {ts.failure_reason or 'no reason'}"
        elif ts.manifest_status == "ok":
            reason = "manifest records timestamp_status=ok but manifest.dsse.tsr is absent"
        else:
            reason = "bundle exported without an RFC 3161 timestamp (--timestamp not requested)"
        return _row(element, "missing", reason=reason)
    facts = {
        "tsa_url": ts.tsa_url or "",
        "tsr_sha256": ts.tsr_sha256 or "",
        "pki_status": "" if ts.pki_status is None else str(ts.pki_status),
        "verification": TSR_VERIFY_HINT,
    }
    reason_p: str | None = None
    if _digest(ts.manifest_tsr_sha256) != _digest(ts.tsr_sha256):
        reason_p = "manifest.dsse.tsr digest does not match manifest_dsse_tsr_sha256"
    elif ts.pki_error is not None:
        reason_p = f"TimeStampResp could not be parsed: {ts.pki_error}"
    elif ts.pki_status not in _PKI_GRANTED:
        reason_p = f"TSA did not grant a timestamp (PKIStatus={ts.pki_status})"
    status: RetentionStatus = "complete" if reason_p is None else "partial"
    return _row(element, status, facts=facts, refs=[ref], reason=reason_p)


def audit_row(run_id: str, audit: AuditFacts | None, *, log_ref: str | None) -> RetentionRow:
    """Render the per-run ``audit_trail`` row (create/modify/delete trail, hash-chained)."""
    if audit is None:
        where = f" at {log_ref}" if log_ref else ""
        return _row("audit_trail", "missing", reason=f"no audit log found{where}")
    if not audit.event_types and audit.parse_error is None:
        return _row(
            "audit_trail", "missing", reason=f"no audit-log entries recorded for run {run_id!r}"
        )
    facts = {
        "entries": str(len(audit.event_types)),
        "event_types": ",".join(sorted(set(audit.event_types))),
        "chain_errors": str(audit.chain_errors),
    }
    reason: str | None = None
    if audit.parse_error is not None:
        reason = f"audit log could not be fully parsed: {audit.parse_error}"
    elif audit.chain_errors:
        reason = f"audit hash chain reports {audit.chain_errors} error(s)"
    status: RetentionStatus = "complete" if reason is None else "partial"
    return _row("audit_trail", status, facts=facts, refs=[audit.ref], reason=reason)


def build_retention_attestation(
    *,
    regime: Regime,
    as_of: datetime,
    registry: str | None,
    policy: PolicyFacts | None,
    holds: HoldFacts | None,
    bundles: Iterable[BundleFacts],
) -> RetentionAttestation:
    """Render the NF-277 retention-posture attestation (pure; ``as_of`` is injected).

    Raises:
        ValueError: ``as_of`` is naive (WORM lock expiry needs an aware instant).
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    posture = [policy_row(registry, policy), hold_row(registry, holds)]
    artifacts = [
        ArtifactRetention(
            bundle=b.bundle,
            bundle_id=b.bundle_id,
            run_id=b.run_id,
            rows=[
                worm_row(b.run_id, b.worm, as_of=as_of),
                timestamp_row(b.bundle, b.timestamp),
                audit_row(b.run_id, b.audit, log_ref=b.audit_log_ref),
            ],
        )
        for b in bundles
    ]
    summary = {"complete": 0, "partial": 0, "missing": 0}
    for row in [*posture, *(r for a in artifacts for r in a.rows)]:
        summary[row.status] += 1
    return RetentionAttestation(
        regime=REGIME_TEXT[regime],
        banner=FINANCE_HONESTY_BANNER,
        as_of=as_of.isoformat(),
        registry=registry,
        posture=posture,
        artifacts=artifacts,
        summary=summary,
    )
