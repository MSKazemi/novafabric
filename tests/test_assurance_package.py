"""ADR-0166 D5 (first slice) — sealed, re-walkable assessor package + renewal delta.

A ``third_party_assessor_package`` (NF-349) is a self-contained bundle carrying everything an
assessor needs to **re-walk the argument offline** — the argument graph, each bound capsule root,
the conformance map, the currency ledger, open defeaters, and a coverage metric — and *nothing that
decides the outcome* (no verdict). A ``renewal_delta`` (NF-350) records what moved since a prior
sealed package so a re-assessment sees exactly the change.

This first slice is the **pure model + deterministic digest + delta computation** — composing the
D1–D4 models. It does not seal: per the ADR, sealing reuses the existing Evidence-Bundle path
(wiring deferred), and this adds no new capsule-schema field and no new serialization format.
"""
from __future__ import annotations

from novafabric.assure.case import AssuranceCase, AssuranceNode, EvidenceRef, NodeType
from novafabric.assure.conformance import ConformanceMap, ConformanceMapEntry, Standard
from novafabric.assure.defeater import Defeater, DefeaterState
from novafabric.assure.package import (
    AssessorPackage,
    BoundCapsule,
    RenewalDelta,
    build_assessor_package,
    compute_renewal_delta,
    package_digest,
)


def _case(*, extra_solution: bool = False, evidence_digest: str = "a" * 64) -> AssuranceCase:
    nodes = [
        AssuranceNode(id="G1", type=NodeType.goal, statement="safe", supported_by=["S1"]),
        AssuranceNode(id="S1", type=NodeType.solution, statement="evidence",
                      evidence_refs=[EvidenceRef(ref="capsule://run-1", digest=evidence_digest)]),
    ]
    if extra_solution:
        nodes[0].supported_by.append("S2")
        nodes.append(AssuranceNode(id="S2", type=NodeType.solution, statement="uncovered"))
    return AssuranceCase(case_id="C1", nodes=nodes)


def test_package_carries_the_components_and_no_verdict():
    pkg = build_assessor_package(package_id="P1", case=_case())
    assert isinstance(pkg, AssessorPackage)
    assert pkg.case.case_id == "C1"
    assert "verdict" not in AssessorPackage.model_fields  # carries evidence, never a decision


def test_open_defeaters_only_are_carried():
    defeaters = [
        Defeater(id="D1", target_node_id="G1", statement="x"),
        Defeater(id="D2", target_node_id="S1", statement="y",
                 state=DefeaterState.rebutted, resolved_by="e" * 64),
    ]
    pkg = build_assessor_package(package_id="P1", case=_case(), defeaters=defeaters)
    assert [d.id for d in pkg.open_defeaters] == ["D1"]


def test_coverage_is_supported_solutions_over_total():
    # one solution with evidence, one without → coverage 0.5
    pkg = build_assessor_package(package_id="P1", case=_case(extra_solution=True))
    assert pkg.coverage == 0.5


def test_bound_capsules_are_carried():
    caps = [BoundCapsule(capsule_root="f" * 64, inclusion_proof=["a" * 64, "b" * 64])]
    pkg = build_assessor_package(package_id="P1", case=_case(), bound_capsules=caps)
    assert pkg.bound_capsules[0].capsule_root == "f" * 64


def test_package_digest_is_deterministic_and_content_bound():
    p1 = build_assessor_package(package_id="P1", case=_case())
    p2 = build_assessor_package(package_id="P1", case=_case())
    assert package_digest(p1) == package_digest(p2)
    # a different evidence digest changes the package digest
    p3 = build_assessor_package(package_id="P1", case=_case(evidence_digest="b" * 64))
    assert package_digest(p3) != package_digest(p1)


def test_renewal_delta_reports_added_nodes_and_defeater_moves():
    prior = build_assessor_package(
        package_id="P1", case=_case(),
        defeaters=[Defeater(id="D1", target_node_id="G1", statement="x")],
    )
    current = build_assessor_package(
        package_id="P2", case=_case(extra_solution=True),  # S2 added
        defeaters=[Defeater(id="D1", target_node_id="G1", statement="x",
                            state=DefeaterState.withdrawn)],  # D1 now closed
    )
    delta = compute_renewal_delta(prior, current)
    assert isinstance(delta, RenewalDelta)
    assert delta.prior_package_digest == package_digest(prior)
    assert "S2" in delta.nodes_added
    assert "D1" in delta.defeaters_closed
    assert delta.defeaters_opened == []


def test_renewal_delta_reports_refreshed_evidence_and_revised_clauses():
    cmap_a = ConformanceMap(entries=[ConformanceMapEntry(
        node_id="G1", standard=Standard.iso_iec_42001, clause_id="8.3", claim_digest="c" * 64)])
    cmap_b = ConformanceMap(entries=[ConformanceMapEntry(
        node_id="G1", standard=Standard.iso_iec_42001, clause_id="8.3", claim_digest="d" * 64)])
    prior = build_assessor_package(package_id="P1", case=_case(evidence_digest="a" * 64),
                                   conformance_map=cmap_a)
    current = build_assessor_package(package_id="P2", case=_case(evidence_digest="z" * 64),
                                     conformance_map=cmap_b)
    delta = compute_renewal_delta(prior, current)
    assert "S1" in delta.evidence_refreshed          # evidence digest changed on S1
    assert "G1@8.3" in delta.clauses_revised          # claim digest changed for the clause
