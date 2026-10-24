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

import base64
from dataclasses import dataclass
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


class SignatureMaterial(_Model):
    """The signature itself, so a checker can re-verify it (review cli-a-01).

    ``public_key_pem`` is what the block states — self-asserted. A checker verifies
    the signature under a key *it* trusts and never under this one.
    """

    alg: Literal["ed25519"]
    public_key_pem: str
    signature_b64: str
    signed: Literal["hash_at_acquisition"] = "hash_at_acquisition"


class Certification(_Model):
    process_description: str
    qualified_person: str | None
    digital_signature_alg: str | None
    signature_verifies: bool
    #: Additive. Absent in blocks written before it existed, which therefore
    #: cannot be re-verified and never pass :func:`reverify_admissibility`.
    signature: SignatureMaterial | None = None


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
    signature: SignatureMaterial | None = None,
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
            signature=signature,
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
    material: SignatureMaterial | None = None
    if signer is not None:
        from novafabric.evidence.signing import verify_with_pem

        message = coc.acquisition.hash_at_acquisition.encode()
        signature = signer.sign(message)
        signature_verifies = verify_with_pem(signer.public_pem, message, signature)
        alg = "ed25519"
        public_pem = signer.public_pem
        material = SignatureMaterial(
            alg="ed25519",
            public_key_pem=public_pem.decode() if isinstance(public_pem, bytes) else public_pem,
            signature_b64=base64.b64encode(signature).decode("ascii"),
        )
    elif coc.custodian.provenance != "operator_declared":
        # Review cli-a-01 / I3: a custodian named as OIDC- or NovaSeal-witnessed with
        # nothing that vouches for it is the operator's word — record it as such.
        coc.custodian = coc.custodian.model_copy(update={"provenance": "operator_declared"})

    sa = build_self_authentication(
        signature_alg=alg,
        signature_verifies=signature_verifies,
        signature=material,
        hash_verified=True,
    )
    status = compute_admissibility_status(coc, sa, timestamp_ok=timestamp_ok)
    return {
        "chain_of_custody": coc.model_dump(),
        "self_authentication": sa.model_dump(),
        "admissibility_status": status,
    }


@dataclass(frozen=True)
class Reverification:
    """What :func:`reverify_admissibility` could witness, point by point."""

    status: AdmissibilityStatus
    points: dict[str, bool]
    reasons: list[str]


def reverify_admissibility(
    block: dict[str, Any],
    *,
    capsule_dir: Path | None,
    trusted_public_pem: bytes | None,
    audit_log_path: Path | None,
    timestamp_ok: bool,
) -> Reverification:
    """Re-witness the five FRE-902(14) points instead of reading the block's booleans.

    Review cli-a-01: the checker re-ran the gate over the block's own fields, so a
    hand-written JSON with them set to ``true`` was "self-authenticating". Here
    every point is recomputed from material the checker controls:

    1. **custodian** — named, not ``operator_declared``, and vouched for by the
       trusted key (point 2): the holder of that key asserts who the custodian is;
    2. **signature** — the carried signature verifies under *trusted_public_pem*
       over ``hash_at_acquisition`` (the block's own public key is not trusted);
    3. **hash** — *capsule_dir*'s Merkle root equals ``hash_at_acquisition``;
    4. **timestamp** — the checker's own *timestamp_ok* assertion;
    5. **custody** — the local hash-chained audit log at *audit_log_path* holds
       exactly the block's custody events for this run, and its chain verifies.

    Anything the checker was not given fails its point, so a block alone can
    never be self-authenticating.
    """
    reasons: list[str] = []
    try:
        coc = ChainOfCustody.model_validate(block.get("chain_of_custody"))
        sa = SelfAuthentication.model_validate(block.get("self_authentication"))
    except ValueError as exc:
        return Reverification("incomplete", {}, [f"not a custody block: {exc}"])

    hash_at_acquisition = coc.acquisition.hash_at_acquisition
    material = sa.certification.signature
    signature_ok = False
    if material is None:
        reasons.append("signature: the block carries no signature to re-verify")
    elif trusted_public_pem is None:
        reasons.append("signature: no trusted public key given (--pubkey)")
    else:
        from novafabric.evidence.signing import verify_with_pem  # noqa: PLC0415

        try:
            signature_ok = verify_with_pem(
                trusted_public_pem,
                hash_at_acquisition.encode(),
                base64.b64decode(material.signature_b64, validate=True),
            )
        except (ValueError, TypeError):
            signature_ok = False
        if not signature_ok:
            reasons.append("signature: does not verify under the trusted key")

    custodian_ok = (
        coc.custodian.identity is not None
        and coc.custodian.provenance != "operator_declared"
        and signature_ok
    )
    if not custodian_ok:
        reasons.append(
            "custodian: operator_declared, unnamed, or not vouched for by the trusted key"
        )

    hash_ok = False
    custody_ok = False
    if capsule_dir is None:
        reasons.append("hash: no capsule given (--capsule) to recompute it from")
        reasons.append("custody: no capsule given (--capsule) to look its run up")
    else:
        hash_ok = capsule_merkle_root(capsule_dir) == hash_at_acquisition
        if not hash_ok:
            reasons.append("hash: the capsule's Merkle root is not hash_at_acquisition")
        if audit_log_path is None or not audit_log_path.is_file():
            reasons.append("custody: no audit log to check the custody events against")
        else:
            rebuilt = build_chain_of_custody(
                capsule_dir, audit_log_path=audit_log_path, run_id=capsule_dir.name
            )
            custody_ok = rebuilt.integrity_continuous and [
                e.entry_hash for e in rebuilt.custody_events
            ] == [e.entry_hash for e in coc.custody_events]
            if not custody_ok:
                reasons.append(
                    "custody: the audit log does not hold exactly these events, "
                    "or its hash chain is broken"
                )
    if not timestamp_ok:
        reasons.append("timestamp: not asserted (--timestamp-ok)")

    points = {
        "custodian": custodian_ok,
        "signature": signature_ok,
        "hash": hash_ok,
        "timestamp": timestamp_ok,
        "custody": custody_ok,
    }
    status: AdmissibilityStatus = (
        "self-authenticating" if all(points.values()) else "requires-foundation"
    )
    return Reverification(status, points, reasons)


__all__ = [
    "Acquisition",
    "Reverification",
    "SignatureMaterial",
    "reverify_admissibility",
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
