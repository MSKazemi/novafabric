"""ADR-0169 D1 / NF-374 — FOIA / public-records-grade decision export (DRAFT-crosswalk half).

A complete, ordered, redaction-aware record of an agent decision: ``decision_ref``, an ordered
``record_index`` of included capsule/artifact digests, ``redactions`` (each a *salted digest* +
``exemption_ref`` naming the **claimed** statutory exemption — never NovaFabric's judgment), and a
deterministic ``custody_digest`` chaining the export to the sealed decision. Redacted bytes are
**absent** — a redaction carries only a digest, never the withheld content. (The selective-disclosure
*prove-without-revealing* crypto is NF-376, gated on ADR-0151, and is not part of this slice.)
"""
from __future__ import annotations

from novafabric.compliance.export.public._foia import (
    FOIAExport,
    FOIARedaction,
    build_foia_export,
)


def test_status_is_draft_and_decision_ref_bound():
    rec = build_foia_export(decision_ref="capsule://root#dec")
    assert isinstance(rec, FOIAExport)
    assert rec.status == "DRAFT"
    assert rec.decision_ref == "capsule://root#dec"
    assert rec.record_index == []
    assert rec.redactions == []
    assert rec.custody_digest  # still computed over an empty record


def test_record_index_order_is_preserved_not_sorted():
    order = ["z#1", "a#2", "m#3"]
    rec = build_foia_export(decision_ref="d", record_index=order)
    assert rec.record_index == order  # ordered record — never re-sorted


def test_redaction_carries_digest_and_exemption_only_bytes_absent():
    rec = build_foia_export(
        decision_ref="d",
        redactions=[{"digest": "salted:abc123", "exemption_ref": "FOIA(b)(6) — personal privacy"}],
    )
    r = rec.redactions[0]
    assert r.digest == "salted:abc123"
    assert r.exemption_ref == "FOIA(b)(6) — personal privacy"
    # No content/value/body field on the model — the withheld bytes are absent (I-5).
    for forbidden in ("value", "content", "body", "plaintext"):
        assert forbidden not in FOIARedaction.model_fields


def test_no_exemption_judgment_or_verdict_field():
    # exemption_ref is the CLAIMED exemption; NovaFabric never adjudicates whether it holds.
    for forbidden in ("exemption_valid", "granted", "verdict", "lawful", "approved"):
        assert forbidden not in FOIAExport.model_fields
        assert forbidden not in FOIARedaction.model_fields


def test_custody_digest_is_deterministic_and_content_bound():
    a1 = build_foia_export(decision_ref="d", record_index=["x#1", "y#2"])
    a2 = build_foia_export(decision_ref="d", record_index=["x#1", "y#2"])
    b = build_foia_export(decision_ref="d", record_index=["x#1", "y#3"])  # one digest differs
    assert a1.custody_digest == a2.custody_digest  # deterministic
    assert a1.custody_digest != b.custody_digest  # binds the record contents
    assert len(a1.custody_digest) == 64  # sha256 hex


def test_custody_digest_binds_the_decision_ref():
    a = build_foia_export(decision_ref="d1", record_index=["x#1"])
    b = build_foia_export(decision_ref="d2", record_index=["x#1"])
    assert a.custody_digest != b.custody_digest  # chained to the sealed decision
