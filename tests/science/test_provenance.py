# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""ADR-0164 P1 — science-provenance facet + hypothesis→result DAG (NF-321).

Tests are organised by the ADR's invariants, because those are what a reviewer
needs to be convinced of: I-1 additive-first, I-2 no payloads, I-3 fail-open,
I-4 records-claims-does-not-adjudicate — plus the DAG section, which is the
substance of this slice.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from novafabric.science import (
    CyclicLineageError,
    DagTooLargeError,
    DagVerification,
    DuplicateNodeError,
    InvalidNodeDigestError,
    PayloadCaptureError,
    ScienceNode,
    ScienceProvenanceFacet,
    UnresolvedParentError,
    attach_facet,
    build_dag,
    build_facet,
    digest_node,
    facet_from_capsule,
    verify_dag,
    verify_node_digest,
)
from novafabric.science.provenance import FACET_NAME, MAX_IDENTIFIER_LENGTH

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "run-capsule.schema.json"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "science-provenance"

#: The unpublished research content this facet must never carry (I-2).
PROTOCOL = "incubate 90 min at 37C with 2 uM compound X, then lyse"


def _node(kind: str, node_id: str, content: str, **kw: object) -> ScienceNode:
    return ScienceNode(
        kind=kind,  # type: ignore[arg-type]
        node_id=node_id,
        node_digest=digest_node(content),
        **kw,  # type: ignore[arg-type]
    )


def _chain() -> list[ScienceNode]:
    """The ADR-0164 §4.1 shape: a single-parent hypothesis→claim chain."""
    kinds = [
        "hypothesis",
        "experiment_design",
        "experiment_run",
        "observation",
        "result",
        "claim",
    ]
    nodes: list[ScienceNode] = []
    parent: str | None = None
    for index, kind in enumerate(kinds):
        node = _node(kind, f"N{index}", f"content-{index}", parent=parent)
        nodes.append(node)
        parent = node.node_digest
    return nodes


# ── Golden fixtures (the two the ADR's P1 bullet names) ───────────────────


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture
def non_science_capsule() -> dict[str, Any]:
    return json.loads((FIXTURES / "valid-non-science-capsule.json").read_text())


def test_golden_non_science_capsule_validates_against_the_real_schema(
    schema: dict[str, Any], non_science_capsule: dict[str, Any]
) -> None:
    """Golden fixture 1: a capsule that is not science is untouched by this ADR."""
    assert "facets" not in non_science_capsule
    jsonschema.validate(non_science_capsule, schema)


def test_golden_facet_round_trips_through_the_models() -> None:
    """Golden fixture 2: the on-disk facet parses, and re-emits identically.

    Round-tripping rather than merely parsing: a fixture that only parses can
    drift from what the builder writes without anything noticing.
    """
    blob = json.loads((FIXTURES / "valid-facet.json").read_text())
    facet = ScienceProvenanceFacet.model_validate(blob)
    assert facet.model_dump(exclude_none=True) == blob
    assert [n.kind for n in facet.hypothesis_experiment_result] == [
        "hypothesis",
        "experiment_design",
        "experiment_run",
        "observation",
        "result",
        "claim",
    ]


def test_golden_facet_uses_the_adr_single_parent_wire_shape() -> None:
    """The spec writes `parent` as a scalar digest; that must stay valid input."""
    blob = json.loads((FIXTURES / "valid-facet.json").read_text())
    parents = [n.get("parent") for n in blob["hypothesis_experiment_result"]]
    assert parents[0] is None
    assert all(isinstance(p, str) for p in parents[1:])


# ── Schema conformance: a facet-bearing capsule against the REAL schema ───
#
# Five earlier facet slices (ADR-0142/0145/0152/0153/0163) shipped code whose
# tests used plain dicts and never validated against run-capsule.schema.json —
# so they all wrote a capsule the schema rejected. These tests use the real
# schema and the real builder, for exactly that reason.


def _science_provenance_is_registered(schema: dict[str, Any]) -> bool:
    facets = schema.get("properties", {}).get("facets", {})
    return FACET_NAME in facets.get("properties", {})


def test_facet_name_is_registered_in_the_capsule_schema(
    schema: dict[str, Any],
) -> None:
    """The facet registry is closed (ADR-0196 D2): an unregistered name is invalid.

    Registration lands in `schemas/run-capsule.schema.json`, which this slice
    does not own and must not edit. Skipping rather than failing keeps this
    slice's gates honest about what it controls; the assertion below is what
    turns registration into a checked fact the moment it exists.
    """
    if not _science_provenance_is_registered(schema):
        pytest.skip(
            f"{FACET_NAME!r} not yet registered in run-capsule.schema.json "
            "(ADR-0196 registry); this slice does not own that file"
        )
    assert schema["properties"]["facets"]["properties"][FACET_NAME]["type"] == "object"


def test_builder_output_validates_against_the_real_schema(
    schema: dict[str, Any], non_science_capsule: dict[str, Any]
) -> None:
    """The regression the five earlier slices shipped: builder output vs. schema."""
    if not _science_provenance_is_registered(schema):
        pytest.skip(f"{FACET_NAME!r} not yet registered in run-capsule.schema.json")
    out = attach_facet(non_science_capsule, build_facet(_chain()))
    jsonschema.validate(out, schema)


def test_no_material_attach_is_a_no_op_and_still_validates(
    schema: dict[str, Any], non_science_capsule: dict[str, Any]
) -> None:
    out = attach_facet(non_science_capsule, build_facet([]))
    assert out == non_science_capsule
    jsonschema.validate(out, schema)


# ── The DAG ───────────────────────────────────────────────────────────────


def test_chain_builds_and_orders_ancestors_before_descendants() -> None:
    order = build_dag(_chain())
    assert [n.node_id for n in order] == ["N0", "N1", "N2", "N3", "N4", "N5"]


def test_diamond_is_a_valid_dag_not_a_cycle() -> None:
    """Two parents converging is the case a cycle check must not confuse.

    A result descending from two observations is ordinary science. If the
    acyclicity check flagged it, the check would be unusable on exactly the
    lineages ADR-0164 exists to record.
    """
    root = _node("hypothesis", "H", "h")
    left = _node("observation", "OL", "left", parent=root.node_digest)
    right = _node("observation", "OR", "right", parent=root.node_digest)
    joined = _node(
        "result", "S", "joined", parent=[left.node_digest, right.node_digest]
    )
    order = build_dag([joined, right, left, root])
    ids = [n.node_id for n in order]
    assert ids[0] == "H"
    assert ids[-1] == "S"
    assert set(ids[1:3]) == {"OL", "OR"}
    assert verify_dag([joined, right, left, root]).acyclic is True


def test_self_referencing_node_is_rejected_as_a_cycle() -> None:
    """A node that is its own parent is a cycle of length one."""
    digest = digest_node("h")
    node = ScienceNode(
        kind="hypothesis", node_id="H", node_digest=digest, parent=digest
    )
    with pytest.raises(CyclicLineageError) as excinfo:
        build_dag([node])
    assert excinfo.value.cycle == [digest, digest]


def test_two_node_cycle_is_rejected_and_names_the_path() -> None:
    a_digest, b_digest = digest_node("a"), digest_node("b")
    a = ScienceNode(
        kind="hypothesis", node_id="A", node_digest=a_digest, parent=b_digest
    )
    b = ScienceNode(kind="claim", node_id="B", node_digest=b_digest, parent=a_digest)
    with pytest.raises(CyclicLineageError) as excinfo:
        build_dag([a, b])
    cycle = excinfo.value.cycle
    assert cycle[0] == cycle[-1]
    assert set(cycle) == {a_digest, b_digest}


def test_longer_cycle_is_rejected_and_the_message_names_every_hop() -> None:
    """"This DAG has a cycle" is not actionable; "H -> D -> R -> H" is."""
    digests = [digest_node(f"n{i}") for i in range(4)]
    nodes = [
        ScienceNode(
            kind="observation",
            node_id=f"N{i}",
            node_digest=digests[i],
            parent=digests[(i - 1) % 4],
        )
        for i in range(4)
    ]
    with pytest.raises(CyclicLineageError) as excinfo:
        build_dag(nodes)
    assert set(excinfo.value.cycle) == set(digests)
    for digest in digests:
        assert digest in str(excinfo.value)


def test_a_cycle_elsewhere_does_not_hide_behind_a_clean_chain() -> None:
    """A partly-valid facet must still fail: Kahn's algorithm emits the clean part."""
    clean = _chain()
    a_digest, b_digest = digest_node("cyc-a"), digest_node("cyc-b")
    cyc_a = ScienceNode(
        kind="result", node_id="CA", node_digest=a_digest, parent=b_digest
    )
    cyc_b = ScienceNode(
        kind="claim", node_id="CB", node_digest=b_digest, parent=a_digest
    )
    with pytest.raises(CyclicLineageError):
        build_dag([*clean, cyc_a, cyc_b])


def test_unresolved_parent_is_surfaced_not_dropped() -> None:
    """Dropping a dangling edge would make an incomplete DAG look complete.

    That is the one failure mode this module must never have: a verifier that
    silently omits an unresolvable ancestor reports a clean lineage over a
    broken one.
    """
    missing = digest_node("a node that is not in this facet")
    orphan = _node("result", "S", "orphan", parent=missing)
    with pytest.raises(UnresolvedParentError) as excinfo:
        build_dag([orphan])
    assert excinfo.value.node_id == "S"
    assert excinfo.value.parent == missing


def test_verify_records_an_unresolved_parent_instead_of_raising() -> None:
    """The report-don't-abort path a verifier walking someone else's capsule needs."""
    orphan = _node("result", "S", "orphan", parent=digest_node("missing"))
    flags = verify_dag([orphan])
    assert flags.dag_walk_ok is False
    assert flags.no_broken_parent is False
    # Acyclicity was never established, so it is `None` — not `True`.
    assert flags.acyclic is None


def test_verify_distinguishes_a_cycle_from_a_broken_parent() -> None:
    digest = digest_node("h")
    node = ScienceNode(
        kind="hypothesis", node_id="H", node_digest=digest, parent=digest
    )
    flags = verify_dag([node])
    assert flags.acyclic is False
    # Every parent resolved — that is how the walk got far enough to find a cycle.
    assert flags.no_broken_parent is True


def test_verify_reports_a_clean_chain() -> None:
    flags = verify_dag(_chain())
    assert (flags.dag_walk_ok, flags.acyclic, flags.no_broken_parent) == (
        True,
        True,
        True,
    )
    # P1 checks the DAG and nothing else; the seal flag stays unchecked.
    assert flags.sealed_into_root is None


def test_duplicate_node_digests_are_rejected_as_ambiguous() -> None:
    digest = digest_node("same content")
    first = ScienceNode(kind="observation", node_id="A", node_digest=digest)
    second = ScienceNode(kind="observation", node_id="B", node_digest=digest)
    with pytest.raises(DuplicateNodeError):
        build_dag([first, second])


def test_oversize_lineage_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """An offline verifier walks capsules it did not produce; the walk is bounded."""
    from novafabric.science import provenance

    monkeypatch.setattr(provenance, "MAX_DAG_NODES", 3)
    with pytest.raises(DagTooLargeError):
        build_dag([_node("observation", f"N{i}", f"c{i}") for i in range(5)])


def test_empty_lineage_is_an_empty_dag_not_an_error() -> None:
    assert build_dag([]) == []


# ── Deterministic serialisation ───────────────────────────────────────────


def test_node_order_is_independent_of_input_order() -> None:
    """The facet is hashed into the seal; a stable order keeps that hash comparable."""
    nodes = _chain()
    assert [n.node_id for n in build_dag(nodes)] == [
        n.node_id for n in build_dag(list(reversed(nodes)))
    ]


def test_independent_siblings_are_ordered_by_digest_not_by_arrival() -> None:
    """The tiebreak is lexicographic on node_digest — and means nothing.

    It is a serialisation guarantee, not a causal claim: two siblings with no
    ancestry relation carry no declared ordering, so any reader treating this
    order as execution order is reading something the producer never said.
    """
    root = _node("hypothesis", "H", "h")
    siblings = [
        _node("observation", f"O{i}", f"obs-{i}", parent=root.node_digest)
        for i in range(4)
    ]
    order = build_dag([root, *siblings])
    tail = [n.node_digest for n in order[1:]]
    assert tail == sorted(tail)


def test_two_runs_declaring_the_same_lineage_produce_the_same_bytes() -> None:
    nodes = _chain()
    a = build_facet(nodes).model_dump_json(exclude_none=True)
    b = build_facet(list(reversed(nodes))).model_dump_json(exclude_none=True)
    assert a == b


# ── I-1: additive-first ───────────────────────────────────────────────────


def test_capsule_without_science_material_is_untouched() -> None:
    """Byte-identical to a capsule captured before this feature existed."""
    capsule = {"run_id": "r", "facets": {"existing": {"a": 1}}}
    assert attach_facet(capsule, build_facet([])) == capsule


def test_attach_does_not_mutate_the_input_capsule() -> None:
    capsule: dict[str, object] = {"run_id": "r"}
    attach_facet(capsule, build_facet(_chain()))
    assert capsule == {"run_id": "r"}


def test_attach_preserves_sibling_facets() -> None:
    capsule = {"run_id": "r", "facets": {"existing": {"a": 1}}}
    out = attach_facet(capsule, build_facet(_chain()))
    assert out["facets"]["existing"] == {"a": 1}
    assert FACET_NAME in out["facets"]


def test_facet_carries_a_schema_version() -> None:
    out = attach_facet({"run_id": "r"}, build_facet(_chain()))
    assert out["facets"][FACET_NAME]["schema_version"]


def test_facet_reads_back_out_of_a_capsule() -> None:
    out = attach_facet({"run_id": "r"}, build_facet(_chain()))
    facet = facet_from_capsule(out)
    assert facet is not None
    assert len(facet.hypothesis_experiment_result) == 6


def test_reading_a_capsule_with_no_facet_is_not_an_error() -> None:
    assert facet_from_capsule({"run_id": "r"}) is None
    assert facet_from_capsule({"run_id": "r", "facets": {}}) is None
    assert facet_from_capsule({"run_id": "r", "facets": "nonsense"}) is None


def test_unknown_fields_survive_for_later_slices() -> None:
    """P2–P5 add receipt/lab/integrity blocks; `extra="allow"` is what lets them."""
    facet = ScienceProvenanceFacet.model_validate(
        {"schema_version": "0.1.0", "reproducibility_receipt": {"seeds": [1337]}}
    )
    assert facet.model_dump()["reproducibility_receipt"] == {"seeds": [1337]}


# ── I-2: no payloads ──────────────────────────────────────────────────────


def test_node_content_is_present_only_as_a_digest() -> None:
    node = _node("experiment_design", "D1", PROTOCOL)
    dumped = build_facet([node]).model_dump_json()
    assert PROTOCOL not in dumped
    assert digest_node(PROTOCOL) in dumped


def test_raw_bytes_are_rejected_rather_than_hashed_for_the_caller() -> None:
    """Hashing bytes here would make smuggling a dataset in effortless and silent."""
    with pytest.raises(PayloadCaptureError):
        ScienceNode(
            kind="observation",
            node_id="O",
            node_digest=PROTOCOL.encode(),  # type: ignore[arg-type]
        )


def test_an_inlined_document_is_rejected_as_a_payload_not_a_digest() -> None:
    with pytest.raises(PayloadCaptureError):
        ScienceNode(
            kind="observation",
            node_id="O",
            node_digest="x" * (MAX_IDENTIFIER_LENGTH + 1),
        )


def test_an_oversize_identifier_is_rejected() -> None:
    with pytest.raises(PayloadCaptureError):
        _node("observation", "O" * (MAX_IDENTIFIER_LENGTH + 1), "c")


@pytest.mark.parametrize(
    "bad",
    [
        "sha256:short",
        "SHA256:" + "a" * 64,
        "sha256:" + "A" * 64,
        "sha1:" + "a" * 40,
        "https://example.org/hypothesis.json",
        "",
    ],
)
def test_a_non_digest_is_rejected_at_build_time_not_at_audit_time(bad: str) -> None:
    """A locator names where something lives, not what it is; it binds nothing."""
    with pytest.raises(InvalidNodeDigestError):
        ScienceNode(kind="observation", node_id="O", node_digest=bad)


def test_agent_run_ref_is_an_identifier_not_a_document() -> None:
    node = _node("experiment_design", "D1", "c", agent_run_ref="run_017")
    assert node.agent_run_ref == "run_017"
    with pytest.raises(PayloadCaptureError):
        _node("experiment_design", "D2", "c", agent_run_ref=PROTOCOL.encode())


def test_bound_root_must_be_a_content_digest() -> None:
    with pytest.raises(InvalidNodeDigestError):
        build_facet(_chain(), bound_root="https://example.org/root")


# ── I-3: fail-open ────────────────────────────────────────────────────────


def test_absent_material_yields_no_facet_not_an_empty_one() -> None:
    """An empty `science_provenance` block would claim the run is science."""
    out = attach_facet({"run_id": "r"}, build_facet([]))
    assert "facets" not in out


def test_a_malformed_lineage_still_raises_because_that_is_not_absence() -> None:
    """Fail-open covers *missing* material, not *incoherent* material.

    Writing a known-broken DAG into a sealed capsule would be worse evidence
    than writing none: the seal would attest to a lineage nobody can walk.
    """
    digest = digest_node("h")
    with pytest.raises(CyclicLineageError):
        build_facet(
            [
                ScienceNode(
                    kind="hypothesis", node_id="H", node_digest=digest, parent=digest
                )
            ]
        )


# ── I-4: records claims, does not adjudicate them ─────────────────────────


def test_module_exposes_no_experiment_or_judgement_surface() -> None:
    """Record-only is a property of the API, not just of the docs.

    If a run/dispatch/adjudicate entry point ever appears here, this fails —
    which is the point: the ADR-0164 in-mission boundary (NovaFabric never runs
    experiments, controls instruments, or judges validity) should be
    structurally hard to violate.
    """
    import novafabric.science as science

    forbidden = {
        "run_experiment",
        "dispatch",
        "execute",
        "adjudicate",
        "judge",
        "generate_hypothesis",
        "calibrate",
        "reproduce",
    }
    assert forbidden.isdisjoint({name.lower() for name in science.__all__})


def test_verification_flags_default_to_unchecked_not_to_passing() -> None:
    """Absent is not false, and it is certainly not true."""
    flags = DagVerification()
    assert (flags.dag_walk_ok, flags.acyclic) == (None, None)
    assert (flags.no_broken_parent, flags.sealed_into_root) == (None, None)


def test_unchecked_flags_are_absent_from_the_serialised_facet() -> None:
    """`null` and "absent" both mean unchecked, but only absence survives cleanly."""
    facet = build_facet(_chain(), verified=DagVerification(dag_walk_ok=True))
    block = attach_facet({"run_id": "r"}, facet)["facets"][FACET_NAME]
    assert block["verified"] == {"dag_walk_ok": True}


def test_digest_check_verifies_identity_and_claims_nothing_more() -> None:
    node = _node("result", "S", "IC50 = 41 nM")
    assert verify_node_digest(node, "IC50 = 41 nM") is True
    assert verify_node_digest(node, "IC50 = 42 nM") is False


def test_building_a_facet_never_authors_a_node() -> None:
    """NovaFabric stores and walks the DAG; it never adds a node of its own."""
    nodes = _chain()
    facet = build_facet(nodes)
    assert {n.node_digest for n in facet.hypothesis_experiment_result} == {
        n.node_digest for n in nodes
    }
