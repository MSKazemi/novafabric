"""ADR-0166 D1 — machine-checkable assurance-case argument graph (first slice).

A GSN/SACM/CAE node graph (goal/strategy/solution/context/assumption/justification)
bound to sealed capsule roots by digest. The validator enforces the structural
invariants — single top goal, acyclic, no orphan, resolvable references — and reports
`solution` nodes with no resolvable evidence as non-fatal `unsupported_leaf`s.
"""
from __future__ import annotations

from novafabric.assure.case import (
    AssuranceCase,
    AssuranceNode,
    EvidenceRef,
    validate_case,
)

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _valid_case() -> AssuranceCase:
    return AssuranceCase(
        case_id="case-1",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="System is acceptably safe",
                          supported_by=["S1"]),
            AssuranceNode(id="S1", type="strategy", statement="Argue over hazards",
                          supported_by=["Sn1"]),
            AssuranceNode(id="Sn1", type="solution", statement="Hazard log evidence",
                          evidence_refs=[EvidenceRef(ref="run:01ABC", digest=DIGEST_A)]),
        ],
    )


def test_valid_case_has_no_errors_and_resolves_its_leaf():
    result = validate_case(_valid_case(), resolvable_digests={DIGEST_A})
    assert result.valid is True
    assert result.errors == []
    assert result.top_goal_id == "G1"
    assert result.unsupported_leaves == []


def test_a_cycle_is_a_fatal_error():
    case = AssuranceCase(
        case_id="c",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="g", supported_by=["S1"]),
            AssuranceNode(id="S1", type="strategy", statement="s", supported_by=["G1"]),
        ],
    )
    result = validate_case(case)
    assert result.valid is False
    assert any("cycle" in e.lower() for e in result.errors)


def test_zero_top_goals_is_a_fatal_error():
    # Every goal is referenced as a child -> no top goal.
    case = AssuranceCase(
        case_id="c",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="g", supported_by=["G2"]),
            AssuranceNode(id="G2", type="goal", statement="g2", supported_by=["G1"]),
        ],
    )
    result = validate_case(case)
    assert result.valid is False
    assert any("top goal" in e.lower() for e in result.errors)


def test_multiple_top_goals_is_a_fatal_error():
    case = AssuranceCase(
        case_id="c",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="g1"),
            AssuranceNode(id="G2", type="goal", statement="g2"),
        ],
    )
    result = validate_case(case)
    assert result.valid is False
    assert any("top goal" in e.lower() for e in result.errors)


def test_orphan_node_is_a_fatal_error():
    case = AssuranceCase(
        case_id="c",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="g", supported_by=["S1"]),
            AssuranceNode(id="S1", type="strategy", statement="s"),
            AssuranceNode(id="X", type="context", statement="unreachable"),  # orphan
        ],
    )
    result = validate_case(case)
    assert result.valid is False
    assert any("orphan" in e.lower() for e in result.errors)


def test_unknown_supported_by_reference_is_a_fatal_error():
    case = AssuranceCase(
        case_id="c",
        nodes=[AssuranceNode(id="G1", type="goal", statement="g", supported_by=["MISSING"])],
    )
    result = validate_case(case)
    assert result.valid is False
    assert any("MISSING" in e for e in result.errors)


def test_duplicate_node_id_is_a_fatal_error():
    case = AssuranceCase(
        case_id="c",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="g", supported_by=["G1"]),
            AssuranceNode(id="G1", type="solution", statement="dup"),
        ],
    )
    result = validate_case(case)
    assert result.valid is False
    assert any("duplicate" in e.lower() for e in result.errors)


def test_solution_with_no_evidence_is_an_unsupported_leaf_but_not_fatal():
    case = AssuranceCase(
        case_id="c",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="g", supported_by=["Sn1"]),
            AssuranceNode(id="Sn1", type="solution", statement="no evidence attached"),
        ],
    )
    result = validate_case(case)
    assert result.valid is True  # structurally valid; the gap is recorded, not fatal
    assert result.unsupported_leaves == ["Sn1"]


def test_solution_whose_digest_does_not_resolve_is_unsupported():
    case = AssuranceCase(
        case_id="c",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="g", supported_by=["Sn1"]),
            AssuranceNode(id="Sn1", type="solution", statement="e",
                          evidence_refs=[EvidenceRef(ref="run:x", digest=DIGEST_B)]),
        ],
    )
    result = validate_case(case, resolvable_digests={DIGEST_A})  # B not resolvable
    assert result.valid is True
    assert result.unsupported_leaves == ["Sn1"]


def test_evidence_ref_binds_by_digest_only_no_body_fields():
    # The reference model carries a ref + digest and nothing that could hold a
    # clause body, finding, or PII (ADR-0166: references and digests only).
    fields = set(EvidenceRef.model_fields)
    assert fields == {"ref", "digest"}


def test_non_solution_leaves_are_not_unsupported():
    # Only `solution` nodes carry evidence; a context/assumption leaf without
    # evidence is normal and must NOT be flagged as an unsupported leaf.
    case = AssuranceCase(
        case_id="c",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="g",
                          supported_by=["C1", "A1", "Sn1"]),
            AssuranceNode(id="C1", type="context", statement="operating context"),
            AssuranceNode(id="A1", type="assumption", statement="assumed input bound"),
            AssuranceNode(id="Sn1", type="solution", statement="evidence",
                          evidence_refs=[EvidenceRef(ref="run:x", digest=DIGEST_A)]),
        ],
    )
    result = validate_case(case, resolvable_digests={DIGEST_A})
    assert result.valid is True
    assert result.unsupported_leaves == []  # C1/A1 are not solutions


def test_empty_case_is_invalid_with_no_top_goal():
    result = validate_case(AssuranceCase(case_id="empty", nodes=[]))
    assert result.valid is False
    assert result.top_goal_id is None
    assert any("top goal" in e.lower() for e in result.errors)
