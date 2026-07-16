"""ADR-0169 D1 / NF-372 — algorithmic-transparency-register crosswalk.

A pure DRAFT crosswalk over already-sealed evidence into the UK ATRS and Amsterdam/Helsinki
algorithm-register record shapes, selected by ``standard`` and standard-version-pinned. Each field
is ``capsule_evidence`` (a ref, never the raw value) or ``operator_declared``; a required field
backed by neither is listed in ``manual_completion_required`` — never fabricated. Output is always a
DRAFT (NovaFabric never registers/publishes).
"""
from __future__ import annotations

import pytest

from novafabric.compliance.export.public._transparency_register import (
    REGISTER_REQUIRED,
    REGISTER_STANDARDS,
    RegisterStandard,
    TransparencyRegisterRecord,
    build_transparency_register,
)


def test_atrs_all_unmapped_when_nothing_supplied():
    rec = build_transparency_register(standard="atrs", capsule_root="r" * 64)
    assert isinstance(rec, TransparencyRegisterRecord)
    assert rec.standard == "atrs"
    assert rec.standard_version == REGISTER_STANDARDS[RegisterStandard.atrs]
    assert rec.fields == []
    assert rec.manual_completion_required == list(REGISTER_REQUIRED[RegisterStandard.atrs])


def test_status_is_always_draft():
    rec = build_transparency_register(standard="amsterdam", capsule_root="r" * 64)
    assert rec.status == "DRAFT"


def test_capsule_evidence_precedence_and_ref_only():
    field = REGISTER_REQUIRED[RegisterStandard.atrs][0]
    rec = build_transparency_register(
        standard="atrs",
        capsule_root="r" * 64,
        operator_declared={field: "declared value"},
        capsule_evidence={field: "capsule://root/f#digest"},
    )
    f = next(x for x in rec.fields if x.name == field)
    assert f.source.value == "capsule_evidence"
    assert f.evidence_ref == "capsule://root/f#digest"
    assert f.value is None  # ref only — never the raw value
    assert field not in rec.manual_completion_required


def test_operator_declared_when_no_capsule_evidence():
    field = REGISTER_REQUIRED[RegisterStandard.amsterdam][0]
    rec = build_transparency_register(
        standard="amsterdam",
        capsule_root="r" * 64,
        operator_declared={field: "the responsible department"},
    )
    f = next(x for x in rec.fields if x.name == field)
    assert f.source.value == "operator_declared"
    assert f.value == "the responsible department"


def test_each_standard_has_its_own_field_set():
    fields = {s: set(REGISTER_REQUIRED[s]) for s in RegisterStandard}
    assert fields[RegisterStandard.atrs] != fields[RegisterStandard.amsterdam]
    assert fields[RegisterStandard.helsinki] != fields[RegisterStandard.amsterdam]
    for s in RegisterStandard:
        assert REGISTER_STANDARDS[s]  # every standard is version-pinned


def test_unknown_standard_is_rejected():
    with pytest.raises(ValueError):
        build_transparency_register(standard="california", capsule_root="r" * 64)


def test_no_registration_or_verdict_field():
    for forbidden in ("registered", "published", "verdict", "compliant", "grade", "score"):
        assert forbidden not in TransparencyRegisterRecord.model_fields
