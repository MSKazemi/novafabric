"""ADR-0161 D1 (first slice) — cross-capsule DSAR assembly over a fleet subject index.

`assemble_dsar` unions the per-capsule records that processed a subject into one deterministic
package: capsules ordered by id (byte-identical on re-run), missing capsules recorded as ``gaps``
(fail-open). It is keyed on the **HMAC pseudonym** — the raw subject id never enters the artifact
(the load-bearing invariant, enforced at the type level).
"""
from __future__ import annotations

from novafabric.compliance.governance.dsar import (
    DSARCapsuleRecord,
    DSARPackage,
    assemble_dsar,
)

_RECS = [
    {"capsule_id": "run-c", "categories": ["email"], "purpose": "support"},
    {"capsule_id": "run-a", "categories": ["name"], "purpose": "billing",
     "redaction_proof_ref": "capsule://run-a/redaction"},
]


def test_package_is_keyed_on_hmac_and_stores_no_raw_subject_id():
    pkg = assemble_dsar("hmac-abc", _RECS)
    assert isinstance(pkg, DSARPackage)
    assert pkg.subject_hmac == "hmac-abc"
    # the raw subject id / direct identifiers must never be fields on the sealed artifact
    for forbidden in ("subject_id", "raw_subject_id", "email", "name", "subject"):
        assert forbidden not in DSARPackage.model_fields


def test_capsules_are_ordered_by_id_and_byte_identical_on_reorder():
    p1 = assemble_dsar("h", _RECS)
    p2 = assemble_dsar("h", list(reversed(_RECS)))
    assert [c.capsule_id for c in p1.capsules] == ["run-a", "run-c"]
    assert p1.model_dump_json() == p2.model_dump_json()


def test_duplicate_capsule_ids_are_collapsed():
    recs = [{"capsule_id": "run-a", "categories": ["name"]},
            {"capsule_id": "run-a", "categories": ["name"]}]
    pkg = assemble_dsar("h", recs)
    assert [c.capsule_id for c in pkg.capsules] == ["run-a"]


def test_gaps_are_deduped_and_sorted():
    pkg = assemble_dsar("h", _RECS, gaps=["run-z", "run-y", "run-z"])
    assert pkg.gaps == ["run-y", "run-z"]


def test_per_capsule_fields_are_carried():
    pkg = assemble_dsar("h", _RECS)
    a = next(c for c in pkg.capsules if c.capsule_id == "run-a")
    assert a.categories == ["name"]
    assert a.purpose == "billing"
    assert a.redaction_proof_ref == "capsule://run-a/redaction"


def test_empty_subject_yields_empty_package():
    pkg = assemble_dsar("h", [])
    assert pkg.capsules == []
    assert pkg.gaps == []


def test_accepts_record_objects_too():
    rec = DSARCapsuleRecord(capsule_id="run-a", categories=["email"])
    pkg = assemble_dsar("h", [rec])
    assert pkg.capsules[0].capsule_id == "run-a"
