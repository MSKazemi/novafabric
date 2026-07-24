"""ADR-0122 D3: the `member_of_session` grouping edge.

A session groups N *otherwise-independent* runs performed in sequence.
That is orthogonal to `contains`/`spawned`/`delegated_to`, which describe
one execution's internal structure — conflating them would let a session
look like a distributed job, the exact confusion ADR-0122 §Context exists
to prevent. These tests pin that separation in code, schema and PROV.
"""

from __future__ import annotations

import json
from pathlib import Path

from novafabric.capsule.prov_mapping import PROV_DM_MAPPING, get_jsonld_context
from novafabric.capsule.schema import EdgeType

_REPO = Path(__file__).resolve().parents[2]


def test_member_of_session_is_a_vocabulary_member() -> None:
    assert EdgeType.member_of_session.value == "member_of_session"


def test_every_edge_type_has_a_prov_mapping() -> None:
    """A new edge type without a PROV mapping would export as nothing."""
    missing = [et.value for et in EdgeType if et not in PROV_DM_MAPPING]
    assert not missing, f"EdgeType(s) with no PROV-DM mapping: {missing}"


def test_member_of_session_maps_to_a_membership_term_not_a_causal_one() -> None:
    """Mapping it causally would assert a relationship ADR-0122 denies.

    Session members are independent runs performed in sequence — one does
    not cause the next. prov:hadMember is the collection-membership term.
    """
    mapping = PROV_DM_MAPPING[EdgeType.member_of_session]
    assert mapping["prov_term"] == "hadMember"
    assert mapping["prov_iri"] == "http://www.w3.org/ns/prov#hadMember"
    causal_terms = {"wasGeneratedBy", "wasInformedBy", "actedOnBehalfOf"}
    assert mapping["prov_term"] not in causal_terms


def test_jsonld_context_carries_the_new_edge() -> None:
    ctx = get_jsonld_context()["@context"]
    assert ctx["member_of_session"] == {"@id": "prov:hadMember"}


def test_lineage_edge_schema_accepts_it() -> None:
    schema = json.loads((_REPO / "schemas" / "lineage-edge.schema.json").read_text())
    assert "member_of_session" in schema["properties"]["edge_type"]["enum"]


def test_parent_child_schema_deliberately_excludes_it() -> None:
    """The separation is structural, not just documentary.

    parent_child_capsule_v1 describes ONE execution's internal hierarchy
    (ADR-0039). A session groups independent runs. Admitting the grouping
    edge there would blur exactly the distinction ADR-0122 draws, so its
    absence is an invariant worth failing on.
    """
    schema = json.loads(
        (_REPO / "schemas" / "parent_child_capsule_v1.schema.json").read_text()
    )
    enum = schema["$defs"]["lineage_edge"]["properties"]["edge_type"]["enum"] if (
        "$defs" in schema and "lineage_edge" in schema.get("$defs", {})
    ) else None
    if enum is None:  # locate the enum wherever it lives
        text = (_REPO / "schemas" / "parent_child_capsule_v1.schema.json").read_text()
        assert "member_of_session" not in text, (
            "member_of_session leaked into the parent/child schema; it is a "
            "grouping edge, orthogonal to one execution's internal structure"
        )
    else:
        assert "member_of_session" not in enum


def test_cli_edge_type_filter_accepts_it() -> None:
    from novafabric.cli.lineage import _VALID_EDGE_TYPES

    assert "member_of_session" in _VALID_EDGE_TYPES
    # ...and the filter stays in step with the vocabulary.
    assert _VALID_EDGE_TYPES == {et.value for et in EdgeType}
