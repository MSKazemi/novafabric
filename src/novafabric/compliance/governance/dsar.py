"""Cross-capsule DSAR assembly over a fleet subject index (ADR-0161 D1, first slice).

`assemble_dsar` unions the per-capsule records that processed a subject into one
:class:`DSARPackage`: capsules ordered by id so a re-run over the same sealed inputs is
**byte-identical**, and capsules known to process the subject but not found are recorded as
``gaps`` (fail-open) rather than raising.

**Load-bearing invariant (ADR-0161 D1): the package is keyed on the HMAC pseudonym; the raw
subject id never enters the artifact.** :class:`DSARPackage` has a ``subject_hmac`` field and no
raw-identifier field, so a direct identifier cannot be serialized into the sealed evidence.

This first slice is the pure deterministic assembler over supplied records. The collector that
gathers them — unioning the shipped per-capsule ``redaction_subject_idx`` (``compliance/pii/index``)
by ``subject_id_hmac`` and reading each capsule's categories/purpose/redaction-proof/transfer/
residency/lineage — is a documented follow-on.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel

from novafabric.compliance.export.provenance import EvidenceSource

#: ADR-0197 provenance of a DSAR gap: a capsule known to process the subject but
#: not reconstructable is an attempted-and-failed resolution → ``unverifiable``
#: (I-2: a checked absence, never downgraded to an operator assertion).
DSAR_GAP_EVIDENCE_SOURCE = EvidenceSource.unverifiable


class DSARCapsuleRecord(BaseModel):
    capsule_id: str
    categories: list[str] = []  # data categories processed for the subject in this capsule
    purpose: str | None = None
    redaction_proof_ref: str | None = None  # reference to the sealed redaction proof, never a value
    transfer_basis: str | None = None
    residency: str | None = None
    lineage_neighbours: list[str] = []
    # ADR-0197: the first-slice assembler projects supplied records without
    # re-performing any seal, so a record is operator_asserted (never
    # capsule_verified). A caller that verifies redaction_proof_ref may upgrade it.
    evidence_source: EvidenceSource = EvidenceSource.operator_asserted


class DSARPackage(BaseModel):
    subject_hmac: str  # HMAC pseudonym — the raw subject id NEVER enters this artifact
    capsules: list[DSARCapsuleRecord]
    gaps: list[str]  # capsule ids known to process the subject but not reconstructable
    #: Provenance of every entry in ``gaps`` (ADR-0197). See DSAR_GAP_EVIDENCE_SOURCE.
    gap_evidence_source: EvidenceSource = DSAR_GAP_EVIDENCE_SOURCE
    # Intentionally NO raw subject-id / direct-identifier field (ADR-0161 D1 invariant).


def assemble_dsar(
    subject_hmac: str,
    records: Iterable[Mapping[str, Any] | DSARCapsuleRecord],
    *,
    gaps: Iterable[str] = (),
) -> DSARPackage:
    """Assemble a subject's cross-capsule DSAR package, deterministically.

    Records may be mappings or :class:`DSARCapsuleRecord` objects. Duplicate ``capsule_id`` entries
    are collapsed (first occurrence wins) and the result is ordered by ``capsule_id`` — a total
    order that makes re-runs byte-identical. ``gaps`` are deduplicated and sorted for the same
    reason.
    """
    by_id: dict[str, DSARCapsuleRecord] = {}
    for rec in records:
        record = (
            rec if isinstance(rec, DSARCapsuleRecord) else DSARCapsuleRecord.model_validate(rec)
        )
        by_id.setdefault(record.capsule_id, record)
    capsules = [by_id[cid] for cid in sorted(by_id)]
    return DSARPackage(subject_hmac=subject_hmac, capsules=capsules, gaps=sorted(set(gaps)))
