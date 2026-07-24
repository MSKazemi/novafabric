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

"""ADR-0166 D1/P1 — assurance-case argument graph + leaf binding (NF-341/342).

Organised by the four invariants the ADR turns on:

- **I-1 Additive-first** — a capsule without a case is untouched and stays valid.
- **I-2 No payloads** — evidence binds by reference and digest, nothing else.
- **I-3 Absent is not false** — an unresolved binding is an ``unsupported_leaf``,
  never "satisfied" and never "refuted".
- **I-4 Structure only** — the checks are structural; nothing here rules the
  argument sound.

The structural checks get their own section, because the graph is the substance
of this slice: acyclicity (self-, 2-, and longer cycles), exactly one top goal
(zero and two are distinct errors), and orphans surfaced rather than dropped.

``tests/test_assurance_case.py`` covers the same validator from the D1 first
slice and is left as-is; these tests cover the hardened traversal, the named
errors, and the facet half that P1 added.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from novafabric.assure.case import (
    FACET_NAME,
    MAX_CASE_NODES,
    AssuranceCase,
    AssuranceCaseError,
    AssuranceNode,
    CaseTooLargeError,
    CyclicArgumentError,
    DuplicateNodeIdError,
    EvidenceRef,
    MultipleTopGoalsError,
    NoTopGoalError,
    OrphanNodeError,
    UnresolvedNodeRefError,
    attach_facet,
    build_case,
    build_facet,
    facet_from_capsule,
    validate_case,
    verify_case,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "run-capsule.schema.json"
BASELINE = (
    REPO_ROOT / "tests" / "fixtures" / "model-provenance" / "valid-text-only-capsule.json"
)

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture
def capsule() -> dict[str, Any]:
    return json.loads(BASELINE.read_text())


def _valid_case() -> AssuranceCase:
    """The ADR's golden fixture: a minimal, complete, resolvable argument."""
    return AssuranceCase(
        case_id="case-1",
        nodes=[
            AssuranceNode(
                id="G1",
                type="goal",
                statement="System is acceptably safe",
                supported_by=["S1"],
            ),
            AssuranceNode(
                id="S1", type="strategy", statement="Argue over hazards", supported_by=["Sn1"]
            ),
            AssuranceNode(
                id="Sn1",
                type="solution",
                statement="Hazard log evidence",
                evidence_refs=[EvidenceRef(ref="run:01ABC", digest=DIGEST_A)],
            ),
        ],
    )


def _diamond_case() -> AssuranceCase:
    """Two strategies converging on one solution — a DAG, emphatically not a cycle."""
    return AssuranceCase(
        case_id="diamond",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="safe", supported_by=["S1", "S2"]),
            AssuranceNode(id="S1", type="strategy", statement="by hazard", supported_by=["Sn1"]),
            AssuranceNode(id="S2", type="strategy", statement="by test", supported_by=["Sn1"]),
            AssuranceNode(
                id="Sn1",
                type="solution",
                statement="shared evidence",
                evidence_refs=[EvidenceRef(ref="run:x", digest=DIGEST_A)],
            ),
        ],
    )


# ── I-1 Additive-first ────────────────────────────────────────────────────


def test_golden_capsule_without_a_case_is_valid_against_the_real_schema(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    """Golden fixture 1 (ADR-0166 P1): no case ⇒ exactly as valid as before."""
    assert "facets" not in capsule
    jsonschema.validate(capsule, schema)


def test_attaching_an_empty_facet_leaves_the_capsule_byte_identical(
    capsule: dict[str, Any],
) -> None:
    """A run that carries no argument must not gain a `facets` key at all."""
    before = copy.deepcopy(capsule)
    facet = build_facet(AssuranceCase(case_id="empty", nodes=[]))

    out = attach_facet(capsule, facet)

    assert out == before
    assert "facets" not in out
    assert json.dumps(out, sort_keys=True) == json.dumps(before, sort_keys=True)


def test_attach_facet_does_not_mutate_the_input_capsule(capsule: dict[str, Any]) -> None:
    before = copy.deepcopy(capsule)
    facet = build_facet(_valid_case(), resolvable_digests={DIGEST_A})

    out = attach_facet(capsule, facet)

    assert capsule == before  # input untouched
    assert out is not capsule  # a new dict, not an in-place edit
    assert FACET_NAME in out["facets"]


def test_attach_facet_preserves_facets_written_by_other_slices(
    capsule: dict[str, Any],
) -> None:
    """The facet container is shared; this slice must not clobber a sibling."""
    capsule["facets"] = {"safety": {"schema_version": "0.1.0"}}

    out = attach_facet(capsule, build_facet(_valid_case(), resolvable_digests={DIGEST_A}))

    assert out["facets"]["safety"] == {"schema_version": "0.1.0"}
    assert out["facets"][FACET_NAME]["case_id"] == "case-1"


def test_golden_facet_bearing_capsule_validates_against_the_real_schema(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    """Golden fixture 2 (ADR-0166 P1): a valid case, validated against the REAL schema.

    Not a hand-written dict: routing through the shipped builder is what would
    have caught the ADR-0196 regression, where five facet slices wrote a
    `facets` key the schema rejected because every test used plain dicts.
    """
    facet = build_facet(_valid_case(), resolvable_digests={DIGEST_A})

    out = attach_facet(capsule, facet)

    jsonschema.validate(out, schema)
    assert out["facets"][FACET_NAME]["top_goal_id"] == "G1"


def test_facet_round_trips_through_a_capsule(capsule: dict[str, Any]) -> None:
    facet = build_facet(_valid_case(), resolvable_digests={DIGEST_A})

    read_back = facet_from_capsule(attach_facet(capsule, facet))

    assert read_back is not None
    assert read_back.case_id == "case-1"
    assert [n.id for n in read_back.nodes] == ["G1", "S1", "Sn1"]
    assert read_back.unsupported_leaves == []


def test_reading_a_facet_from_a_capsule_without_one_is_not_an_error(
    capsule: dict[str, Any],
) -> None:
    assert facet_from_capsule(capsule) is None
    assert facet_from_capsule({}) is None
    assert facet_from_capsule({"facets": "not-a-dict"}) is None
    assert facet_from_capsule({"facets": {}}) is None


# ── I-2 No payloads ───────────────────────────────────────────────────────


def test_evidence_ref_binds_by_reference_and_digest_only() -> None:
    """There must be nowhere to put a clause body, a finding, or assessor PII."""
    assert set(EvidenceRef.model_fields) == {"ref", "digest"}


def test_the_serialised_facet_carries_no_evidence_content(
    capsule: dict[str, Any],
) -> None:
    case = _valid_case()
    case.nodes[2].evidence_refs = [EvidenceRef(ref="run:01ABC", digest=DIGEST_A)]

    out = attach_facet(capsule, build_facet(case, resolvable_digests={DIGEST_A}))

    blob = json.dumps(out["facets"][FACET_NAME])
    assert DIGEST_A in blob  # the binding survives
    assert "run:01ABC" in blob  # the reference survives
    # and nothing that could only have come from dereferencing them
    assert "content" not in blob
    assert "body" not in blob


# ── I-3 Absent is not false ───────────────────────────────────────────────


def test_solution_with_no_binding_is_an_unsupported_leaf_not_a_failure() -> None:
    case = AssuranceCase(
        case_id="c",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="g", supported_by=["Sn1"]),
            AssuranceNode(id="Sn1", type="solution", statement="no evidence attached"),
        ],
    )

    result = validate_case(case)

    assert result.valid is True  # structurally valid; the gap is recorded
    assert result.unsupported_leaves == ["Sn1"]


def test_solution_whose_digest_does_not_resolve_is_unsupported_not_refuted() -> None:
    case = AssuranceCase(
        case_id="c",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="g", supported_by=["Sn1"]),
            AssuranceNode(
                id="Sn1",
                type="solution",
                statement="e",
                evidence_refs=[EvidenceRef(ref="run:x", digest=DIGEST_B)],
            ),
        ],
    )

    result = validate_case(case, resolvable_digests={DIGEST_A})

    assert result.valid is True
    assert result.unsupported_leaves == ["Sn1"]


def test_an_unsupported_leaf_is_recorded_in_the_facet_and_still_builds() -> None:
    """An argument with a gap is exactly what a reviewer needs to SEE."""
    case = _valid_case()

    facet = build_facet(case, resolvable_digests=frozenset())  # nothing resolves

    assert facet.unsupported_leaves == ["Sn1"]
    assert facet.top_goal_id == "G1"
    # The case is still structurally valid — the gap is a finding, not a failure.
    assert facet.verified is not None
    assert facet.verified.graph_walk_ok is True


def test_non_solution_leaves_are_never_unsupported_leaves() -> None:
    """A context/assumption leaf carries no evidence by design."""
    case = AssuranceCase(
        case_id="c",
        nodes=[
            AssuranceNode(
                id="G1", type="goal", statement="g", supported_by=["C1", "A1", "Sn1"]
            ),
            AssuranceNode(id="C1", type="context", statement="operating context"),
            AssuranceNode(id="A1", type="assumption", statement="assumed input bound"),
            AssuranceNode(
                id="Sn1",
                type="solution",
                statement="evidence",
                evidence_refs=[EvidenceRef(ref="run:x", digest=DIGEST_A)],
            ),
        ],
    )

    result = validate_case(case, resolvable_digests={DIGEST_A})

    assert result.valid is True
    assert result.unsupported_leaves == []


def test_digests_resolve_with_or_without_the_sha256_prefix() -> None:
    """The capsule writes `sha256:<hex>`; this module shipped taking bare hex."""
    case = AssuranceCase(
        case_id="c",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="g", supported_by=["Sn1"]),
            AssuranceNode(
                id="Sn1",
                type="solution",
                statement="e",
                evidence_refs=[EvidenceRef(ref="run:x", digest=f"sha256:{DIGEST_A}")],
            ),
        ],
    )

    assert validate_case(case, resolvable_digests={DIGEST_A}).unsupported_leaves == []
    assert (
        validate_case(case, resolvable_digests={f"sha256:{DIGEST_A}"}).unsupported_leaves
        == []
    )
    assert validate_case(case, resolvable_digests={DIGEST_B}).unsupported_leaves == ["Sn1"]


# ── Structure: acyclicity ─────────────────────────────────────────────────


def test_a_node_supporting_itself_is_a_cycle_of_length_one() -> None:
    case = AssuranceCase(
        case_id="c",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="g", supported_by=["S1"]),
            AssuranceNode(id="S1", type="strategy", statement="s", supported_by=["S1"]),
        ],
    )

    with pytest.raises(CyclicArgumentError) as excinfo:
        build_case(case)

    assert excinfo.value.cycle == ["S1", "S1"]
    assert "S1 -> S1" in str(excinfo.value)


def test_a_two_node_cycle_is_reported_with_its_path() -> None:
    case = AssuranceCase(
        case_id="c",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="g", supported_by=["S1"]),
            AssuranceNode(id="S1", type="strategy", statement="s", supported_by=["S2"]),
            AssuranceNode(id="S2", type="strategy", statement="s", supported_by=["S1"]),
        ],
    )

    with pytest.raises(CyclicArgumentError) as excinfo:
        build_case(case)

    assert excinfo.value.cycle == ["S1", "S2", "S1"]


def test_a_longer_cycle_is_reported_with_its_full_path() -> None:
    case = AssuranceCase(
        case_id="c",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="g", supported_by=["A"]),
            AssuranceNode(id="A", type="strategy", statement="a", supported_by=["B"]),
            AssuranceNode(id="B", type="strategy", statement="b", supported_by=["C"]),
            AssuranceNode(id="C", type="strategy", statement="c", supported_by=["A"]),
        ],
    )

    with pytest.raises(CyclicArgumentError) as excinfo:
        build_case(case)

    assert excinfo.value.cycle == ["A", "B", "C", "A"]


def test_validate_case_reports_the_cycle_path_without_raising() -> None:
    """The verifier path reports; it does not abort on someone else's capsule."""
    case = AssuranceCase(
        case_id="c",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="g", supported_by=["S1"]),
            AssuranceNode(id="S1", type="strategy", statement="s", supported_by=["G1"]),
        ],
    )

    result = validate_case(case)

    assert result.valid is False
    assert any("G1 -> S1 -> G1" in e for e in result.errors)


def test_a_diamond_is_a_dag_and_must_not_be_flagged_as_a_cycle() -> None:
    """Two parents converging on one child is convergence, not circularity."""
    result = validate_case(_diamond_case(), resolvable_digests={DIGEST_A})

    assert result.valid is True
    assert result.errors == []
    assert result.top_goal_id == "G1"
    assert result.unsupported_leaves == []


def test_a_diamond_builds_in_deterministic_breadth_first_order() -> None:
    ordered = build_case(_diamond_case())

    assert [n.id for n in ordered] == ["G1", "S1", "S2", "Sn1"]
    # The shared child appears exactly once despite having two parents.
    assert len(ordered) == 4


def test_a_deep_chain_does_not_exhaust_the_stack() -> None:
    """The traversal is iterative: this input would overflow a recursive walk.

    An offline verifier walks capsules it did not produce, so depth is untrusted
    input rather than a property of our own writer.
    """
    depth = 5_000
    nodes = [
        AssuranceNode(id="G1", type="goal", statement="g", supported_by=["N0"]),
        *[
            AssuranceNode(
                id=f"N{i}", type="strategy", statement="s", supported_by=[f"N{i + 1}"]
            )
            for i in range(depth)
        ],
        AssuranceNode(id=f"N{depth}", type="solution", statement="leaf"),
    ]

    result = validate_case(AssuranceCase(case_id="deep", nodes=nodes))

    assert result.valid is True
    assert result.top_goal_id == "G1"


def test_a_case_over_the_node_cap_is_rejected_before_anything_walks_it() -> None:
    """Bounded work: the cap is checked first, not discovered mid-traversal."""
    case = AssuranceCase(case_id="huge", nodes=[])
    # Constructing MAX_CASE_NODES+1 real nodes is wasteful; the check is on the
    # declared length, so a stub list of the right size exercises the same path.
    case.nodes = [
        AssuranceNode(id=f"N{i}", type="goal", statement="g")
        for i in range(MAX_CASE_NODES + 1)
    ]

    with pytest.raises(CaseTooLargeError):
        build_case(case)

    result = validate_case(case)
    assert result.valid is False
    assert any("exceeds" in e for e in result.errors)


# ── Structure: exactly one top goal ───────────────────────────────────────


def test_zero_top_goals_is_its_own_named_error() -> None:
    """Every goal is supported by something ⇒ the argument has no conclusion."""
    case = AssuranceCase(
        case_id="c",
        nodes=[
            AssuranceNode(id="S1", type="strategy", statement="s", supported_by=["G1"]),
            AssuranceNode(id="G1", type="goal", statement="g"),
        ],
    )

    with pytest.raises(NoTopGoalError) as excinfo:
        build_case(case)

    assert "no top goal" in str(excinfo.value)


def test_two_top_goals_is_a_distinct_named_error_naming_both() -> None:
    case = AssuranceCase(
        case_id="c",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="g1"),
            AssuranceNode(id="G2", type="goal", statement="g2"),
        ],
    )

    with pytest.raises(MultipleTopGoalsError) as excinfo:
        build_case(case)

    assert excinfo.value.top_goals == ["G1", "G2"]


def test_zero_and_two_top_goals_do_not_share_an_exception_type() -> None:
    """Distinct messages, per the ADR — they want different fixes."""
    assert NoTopGoalError is not MultipleTopGoalsError
    assert not issubclass(NoTopGoalError, MultipleTopGoalsError)
    assert not issubclass(MultipleTopGoalsError, NoTopGoalError)


def test_an_empty_case_has_no_top_goal() -> None:
    result = validate_case(AssuranceCase(case_id="empty", nodes=[]))

    assert result.valid is False
    assert result.top_goal_id is None
    assert any("top goal" in e.lower() for e in result.errors)


# ── Structure: orphans surfaced, never dropped ────────────────────────────


def test_an_orphan_node_is_surfaced_by_name() -> None:
    case = AssuranceCase(
        case_id="c",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="g", supported_by=["S1"]),
            AssuranceNode(id="S1", type="strategy", statement="s"),
            AssuranceNode(id="X", type="context", statement="unreachable"),
        ],
    )

    with pytest.raises(OrphanNodeError) as excinfo:
        build_case(case)

    assert excinfo.value.orphans == ["X"]


def test_an_orphan_is_never_silently_omitted_from_the_walk() -> None:
    """An incomplete argument must not be able to look complete.

    The failure mode this guards is a builder that quietly drops unreachable
    nodes and emits a facet that reads as a whole argument.
    """
    case = AssuranceCase(
        case_id="c",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="g", supported_by=["S1"]),
            AssuranceNode(id="S1", type="strategy", statement="s"),
            AssuranceNode(id="X", type="solution", statement="unreachable"),
        ],
    )

    with pytest.raises(OrphanNodeError):
        build_facet(case)

    assert "X" in str(validate_case(case).errors)


# ── Structure: identity and references ────────────────────────────────────


def test_a_duplicate_node_id_is_rejected_rather_than_resolved() -> None:
    case = AssuranceCase(
        case_id="c",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="g"),
            AssuranceNode(id="G1", type="solution", statement="dup"),
        ],
    )

    with pytest.raises(DuplicateNodeIdError):
        build_case(case)


def test_an_unresolved_reference_names_the_break() -> None:
    case = AssuranceCase(
        case_id="c",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="g", supported_by=["MISSING"])
        ],
    )

    with pytest.raises(UnresolvedNodeRefError) as excinfo:
        build_case(case)

    assert excinfo.value.node_id == "G1"
    assert excinfo.value.ref == "MISSING"


# ── I-4 Structure only, and the error contract ────────────────────────────


def test_every_named_error_subclasses_the_module_base_but_not_value_error() -> None:
    """Pydantic v2 folds a validator's ValueError into ValidationError.

    Subclassing ValueError would destroy the named type at exactly the boundary
    where a caller most needs to branch on it.
    """
    for exc in (
        CaseTooLargeError,
        DuplicateNodeIdError,
        UnresolvedNodeRefError,
        NoTopGoalError,
        MultipleTopGoalsError,
        OrphanNodeError,
        CyclicArgumentError,
    ):
        assert issubclass(exc, AssuranceCaseError)
        assert issubclass(exc, Exception)
        assert not issubclass(exc, ValueError)


def test_verify_case_reports_flags_without_raising() -> None:
    verification = verify_case(_valid_case())

    assert verification.graph_walk_ok is True
    assert verification.acyclic is True
    assert verification.single_top_goal is True
    assert verification.no_orphan is True


def test_verify_case_leaves_unperformed_checks_as_none_not_false() -> None:
    """Absent is not false: P1 checks no seal, so `sealed_into_root` stays None."""
    assert verify_case(_valid_case()).sealed_into_root is None

    cyclic = AssuranceCase(
        case_id="c",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="g", supported_by=["S1"]),
            AssuranceNode(id="S1", type="strategy", statement="s", supported_by=["G1"]),
        ],
    )
    verification = verify_case(cyclic)
    assert verification.acyclic is False  # checked and failed
    assert verification.no_orphan is None  # never reached, so not asserted either way


def test_the_facet_records_no_soundness_or_score_field() -> None:
    """ADR-0166 alternative 2: a numeric verdict would read as a certification."""
    facet = build_facet(_valid_case(), resolvable_digests={DIGEST_A})

    fields = set(type(facet).model_fields) | set(type(facet.verified).model_fields)
    for banned in ("score", "grade", "sound", "sufficient", "acceptable", "verdict", "pass"):
        assert not any(banned in f for f in fields), f"{banned!r} leaked into the facet"


def test_build_facet_raises_on_a_malformed_case_rather_than_sealing_it() -> None:
    """Fail-open covers missing material, not incoherent material."""
    cyclic = AssuranceCase(
        case_id="c",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="g", supported_by=["S1"]),
            AssuranceNode(id="S1", type="strategy", statement="s", supported_by=["G1"]),
        ],
    )

    with pytest.raises(AssuranceCaseError):
        build_facet(cyclic)
