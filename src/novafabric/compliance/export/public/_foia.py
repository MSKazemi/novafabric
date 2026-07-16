"""FOIA / public-records-grade decision export (ADR-0169 D1 / NF-374, DRAFT-crosswalk first slice).

A pure exporter that assembles a complete, **ordered, redaction-aware** record of an agent decision
over already-sealed evidence:

* ``decision_ref`` — the sealed decision the export is about,
* ``record_index`` — the **ordered** list of included capsule/artifact digests (order preserved,
  never re-sorted: a public record's ordering is part of the record),
* ``redactions`` — each a *salted digest* of the withheld content plus an ``exemption_ref`` naming
  the **claimed** statutory exemption. NovaFabric never adjudicates whether the exemption holds, and
  the withheld bytes are **absent** — a redaction carries a digest, never the content (I-5),
* ``custody_digest`` — a deterministic content digest chaining the export to the sealed decision.

The output ``status`` is always ``DRAFT`` (NovaFabric never files or transmits a records request).
This first slice assembles the record from supplied refs; the collector that gathers the ordered
digests from the sealed capsule set is a documented follow-on. The selective-disclosure
*prove-without-revealing* crypto (Merkle-redaction / SD-JWT / BBS ``root_proof``) is **NF-376**,
gated on ADR-0151, and is deliberately **not** part of this slice — this is the redaction-aware
record, not a cryptographic disclosure proof.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from pydantic import BaseModel


class FOIARedaction(BaseModel):
    digest: str  # salted digest of the withheld content — the bytes themselves are absent (I-5)
    exemption_ref: str  # the CLAIMED statutory exemption — never NovaFabric's judgment


class FOIAExport(BaseModel):
    status: str = "DRAFT"  # NovaFabric never files/transmits a records request
    decision_ref: str
    record_index: list[str]  # ordered digests of included capsule/artifact records
    redactions: list[FOIARedaction]
    custody_digest: str  # deterministic content digest chaining the export to the decision


def _custody_digest(
    decision_ref: str, record_index: list[str], redactions: list[FOIARedaction]
) -> str:
    """Deterministic sha256 over the canonical record, binding the export to the decision.

    ``record_index`` order is preserved (it is part of the record); object keys are canonicalized so
    the digest is stable across runs.
    """
    payload = json.dumps(
        {
            "decision_ref": decision_ref,
            "record_index": record_index,
            "redactions": [
                {"digest": r.digest, "exemption_ref": r.exemption_ref} for r in redactions
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_foia_export(
    *,
    decision_ref: str,
    record_index: Sequence[str] = (),
    redactions: Sequence[Mapping[str, str] | FOIARedaction] = (),
) -> FOIAExport:
    """Assemble a DRAFT FOIA/public-records export from supplied refs and redaction claims.

    ``record_index`` order is preserved. Each redaction is a salted ``digest`` plus the claimed
    ``exemption_ref``; the withheld content is never included. ``custody_digest`` binds the export
    contents to ``decision_ref`` deterministically.
    """
    index = list(record_index)
    red = [
        r if isinstance(r, FOIARedaction) else FOIARedaction.model_validate(r)
        for r in redactions
    ]
    return FOIAExport(
        decision_ref=decision_ref,
        record_index=index,
        redactions=red,
        custody_digest=_custody_digest(decision_ref, index, red),
    )
