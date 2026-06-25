# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for PROV-DM mapping table and JSON-LD context round-trip (FR-08)."""

from __future__ import annotations

from novafabric.capsule.prov_mapping import (
    PROV_DM_MAPPING,
    edge_to_jsonld,
    get_jsonld_context,
    get_mapping_for_edge,
)
from novafabric.capsule.schema import EdgeType
from novafabric.capsule.ulid_util import new_ulid


def test_all_four_edge_types_have_prov_mapping() -> None:
    """FR-08: all four edge types have W3C PROV-DM mapping."""
    for et in EdgeType:
        assert et in PROV_DM_MAPPING, f"Missing PROV-DM mapping for {et}"


def test_spawned_maps_to_was_generated_by() -> None:
    m = PROV_DM_MAPPING[EdgeType.spawned]
    assert m["prov_term"] == "wasGeneratedBy"
    assert "prov#wasGeneratedBy" in m["prov_iri"]


def test_contains_maps_to_was_informed_by() -> None:
    m = PROV_DM_MAPPING[EdgeType.contains]
    assert m["prov_term"] == "wasInformedBy"
    assert "prov#wasInformedBy" in m["prov_iri"]


def test_delegated_to_maps_to_acted_on_behalf_of() -> None:
    m = PROV_DM_MAPPING[EdgeType.delegated_to]
    assert m["prov_term"] == "actedOnBehalfOf"
    assert "prov#actedOnBehalfOf" in m["prov_iri"]


def test_replayed_from_maps_to_novafabric_namespace() -> None:
    m = PROV_DM_MAPPING[EdgeType.replayed_from]
    assert m["prov_term"] == "replayedFrom"
    assert "novafabric.io" in m["prov_iri"]  # NovaFabric-specific


def test_jsonld_context_includes_all_edge_types() -> None:
    context = get_jsonld_context()
    ctx = context["@context"]
    for et in EdgeType:
        assert et.value in ctx, f"Missing {et.value} in JSON-LD context"


def test_edge_to_jsonld_round_trip() -> None:
    """FR-08: parse and round-trip one example edge of each type."""
    for et in EdgeType:
        src = new_ulid()
        tgt = new_ulid()
        eid = new_ulid()
        node = edge_to_jsonld(et, src, tgt, eid)

        # Must have required fields
        assert "@id" in node
        assert "novafabric:edgeType" in node
        assert node["novafabric:edgeType"] == et.value
        assert node["novafabric:sourceRunId"] == src
        assert node["novafabric:targetRunId"] == tgt
        assert "novafabric:provDmTerm" in node


def test_edge_to_jsonld_preserves_attributes() -> None:
    node = edge_to_jsonld(
        EdgeType.delegated_to,
        new_ulid(), new_ulid(), new_ulid(),
        attributes={"delegation_reason": "tool_use"},
    )
    assert node.get("novafabric:attributes") == {"delegation_reason": "tool_use"}


def test_get_mapping_for_edge_returns_correct_type() -> None:
    m = get_mapping_for_edge(EdgeType.contains)
    assert isinstance(m, dict)
    assert "prov_term" in m
    assert "prov_iri" in m
    assert "adr" in m
