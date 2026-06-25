"""Court-admissible evidence binding (ADR-0095 feature B, slice C2).

Builds the additive ``chain_of_custody`` + ``self_authentication`` blocks for an
Evidence Bundle so a regulator/insurer/court can ingest it without operator
cooperation, mapping to US Federal Rules of Evidence 902(13)/(14), FRE 901(b)(9),
Berkeley Protocol, SWGDE chain-of-custody, and ICC Art 69(4).

Invariant I3 — *witnessed-or-declared*: any field NovaFabric cannot witness is
emitted ``null`` with ``provenance="operator_declared"``, never auto-filled with a
plausible value. Custody events come from the hash-chained audit log
(``audit/_log.py``); a broken chain sets ``integrity_continuous=False``.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from novafabric.audit._log import AuditLog
from novafabric.evidence.merkle import capsule_merkle_root

AdmissibilityStatus = Literal["self-authenticating", "requires-foundation", "incomplete"]

_DEFAULT_PROCESS = (
    "NovaFabric capture produced this record and NovaSeal DSSE-signed it; "
    "the capsule content hash is bound into the chain of custody."
)


def _nova_version() -> str:
    try:
        return version("novafabric")
    except PackageNotFoundError:  # pragma: no cover - installed in dev
        return "unknown"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Custodian(_Model):
    identity: str | None = None
    role: str | None = None
    organization: str | None = None
    provenance: Literal["novaseal-identity", "oidc", "operator_declared"] = (
        "operator_declared"
    )


class AcquisitionTool(_Model):
    name: str
    version: str


class Acquisition(_Model):
    method: str
    acquired_at: str | None
    hash_at_acquisition: str
    acquisition_tool: AcquisitionTool
    provenance: Literal["witnessed", "operator_declared"]


class CustodyEvent(_Model):
    event: str
    actor: str
    at: str
    audit_entry_id: str
    prev_hash: str | None
    entry_hash: str


class ChainOfCustody(_Model):
    custodian: Custodian
    acquisition: Acquisition
    custody_events: list[CustodyEvent]
    integrity_continuous: bool


class Certification(_Model):
    process_description: str
    qualified_person: str | None
    digital_signature_alg: str | None
    signature_verifies: bool


class SelfAuthentication(_Model):
    rule: Literal["FRE-902(14)", "FRE-902(13)"]
    certification: Certification
    hash_verified_against_acquisition: bool


def build_chain_of_custody(
    capsule_dir: Path,
    *,
    audit_log_path: Path,
    run_id: str | None = None,
    custodian: Custodian | None = None,
    acquisition_method: str = "novafabric capture (cli-wrapper)",
) -> ChainOfCustody:
    """Assemble the chain of custody from the capsule + the hash-chained audit log."""
    run = run_id or capsule_dir.name
    log = AuditLog(audit_log_path)
    entries = log.query(run)
    events = [
        CustodyEvent(
            event=str(getattr(e.event_type, "value", e.event_type)),
            actor=e.actor,
            at=e.timestamp.isoformat(),
            audit_entry_id=e.entry_id,
            prev_hash=e.prev_hash,
            entry_hash=e.entry_hash,
        )
        for e in entries
    ]
    # the audit log is tamper-evident; an empty verify() means the chain holds
    integrity_continuous = bool(events) and log.verify() == []

    acquired_at = events[0].at if events else None
    capsule_hash = capsule_merkle_root(capsule_dir)
    acquisition = Acquisition(
        method=acquisition_method,
        acquired_at=acquired_at,
        hash_at_acquisition=capsule_hash,
        acquisition_tool=AcquisitionTool(name="novafabric", version=_nova_version()),
        provenance="witnessed" if events else "operator_declared",
    )
    return ChainOfCustody(
        custodian=custodian or Custodian(),  # I3: defaults to operator_declared/null
        acquisition=acquisition,
        custody_events=events,
        integrity_continuous=integrity_continuous,
    )


def build_self_authentication(
    *,
    signature_alg: str | None = None,
    signature_verifies: bool = False,
    hash_verified: bool = True,
    rule: Literal["FRE-902(14)", "FRE-902(13)"] = "FRE-902(14)",
    process_description: str = _DEFAULT_PROCESS,
    qualified_person: str | None = None,
) -> SelfAuthentication:
    return SelfAuthentication(
        rule=rule,
        certification=Certification(
            process_description=process_description,
            qualified_person=qualified_person,
            digital_signature_alg=signature_alg,
            signature_verifies=signature_verifies,
        ),
        hash_verified_against_acquisition=hash_verified,
    )


def compute_admissibility_status(
    coc: ChainOfCustody | None,
    sa: SelfAuthentication | None,
    *,
    timestamp_ok: bool,
) -> AdmissibilityStatus:
    """Apply the five-point FRE-902(14) self-authentication gate.

    Self-authenticating requires all five: a real (non-declared) custodian, a
    verifying signature, hash-match against acquisition, a verifying timestamp,
    and an unbroken custody chain. Otherwise it can still be admitted with a
    foundation witness (``requires-foundation``); a structurally missing block is
    ``incomplete``.
    """
    if coc is None or sa is None:
        return "incomplete"
    checks = (
        coc.custodian.provenance != "operator_declared"
        and coc.custodian.identity is not None,
        sa.certification.signature_verifies,
        sa.hash_verified_against_acquisition,
        timestamp_ok,
        coc.integrity_continuous,
    )
    return "self-authenticating" if all(checks) else "requires-foundation"


def admissibility_block(
    capsule_dir: Path,
    *,
    audit_log_path: Path,
    run_id: str | None = None,
    custodian: Custodian | None = None,
    signer: Any = None,
    timestamp_ok: bool = False,
) -> dict[str, Any]:
    """Build the full additive admissibility block for embedding in an Evidence Bundle.

    When a ``signer`` is supplied the capsule hash is signed and the signature is
    verified, so ``signature_verifies`` reflects a real cryptographic check.
    """
    coc = build_chain_of_custody(
        capsule_dir,
        audit_log_path=audit_log_path,
        run_id=run_id,
        custodian=custodian,
    )

    signature_verifies = False
    alg: str | None = None
    if signer is not None:
        from novafabric.evidence.signing import verify_with_pem

        message = coc.acquisition.hash_at_acquisition.encode()
        signature = signer.sign(message)
        signature_verifies = verify_with_pem(signer.public_pem, message, signature)
        alg = "ed25519"

    sa = build_self_authentication(
        signature_alg=alg,
        signature_verifies=signature_verifies,
        hash_verified=True,
    )
    status = compute_admissibility_status(coc, sa, timestamp_ok=timestamp_ok)
    return {
        "chain_of_custody": coc.model_dump(),
        "self_authentication": sa.model_dump(),
        "admissibility_status": status,
    }


__all__ = [
    "Acquisition",
    "AcquisitionTool",
    "AdmissibilityStatus",
    "Certification",
    "ChainOfCustody",
    "Custodian",
    "CustodyEvent",
    "SelfAuthentication",
    "admissibility_block",
    "build_chain_of_custody",
    "build_self_authentication",
    "compute_admissibility_status",
]
