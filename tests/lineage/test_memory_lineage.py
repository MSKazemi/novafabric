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

"""ADR-0143 P1 — memory lineage edges and poisoned-read back-trace."""

from __future__ import annotations

import pytest

from novafabric.capsule.edge_typer import coerce_legacy_edge_type
from novafabric.capsule.prov_mapping import PROV_DM_MAPPING, get_jsonld_context
from novafabric.capsule.schema import EdgeType
from novafabric.capture.events import MemoryOperationEvent
from novafabric.lineage.memory import (
    edges_for_event,
    edges_for_events,
    memory_node,
    readers_of,
    writers_of,
)

CAPSULE = "01J0000000000000000000CAPS"


def _event(
    operation: str,
    run_id: str,
    key: str = "user_prefs",
    ts: str = "2026-07-20T10:00:00Z",
    **kw: object,
) -> MemoryOperationEvent:
    return MemoryOperationEvent(
        run_id=run_id,
        capsule_id=CAPSULE,
        timestamp_utc=ts,
        operation=operation,  # type: ignore[arg-type]
        memory_key=key,
        **kw,  # type: ignore[arg-type]
    )


# ── Edge vocabulary ───────────────────────────────────────────────────────


def test_edge_types_are_canonical_and_prov_mapped() -> None:
    for name, prov_term in (("wrote_memory", "wasGeneratedBy"), ("read_memory", "used")):
        et = EdgeType(name)
        assert PROV_DM_MAPPING[et]["prov_term"] == prov_term
        assert get_jsonld_context()["@context"][name]["@id"] == f"prov:{prov_term}"


def test_read_and_write_map_to_different_prov_terms() -> None:
    """A read must not claim the reader produced the item."""
    assert (
        PROV_DM_MAPPING[EdgeType.read_memory]["prov_term"]
        != PROV_DM_MAPPING[EdgeType.wrote_memory]["prov_term"]
    )


@pytest.mark.parametrize("name", [e.value for e in EdgeType])
def test_canonical_edge_types_survive_legacy_coercion(name: str) -> None:
    """Regression: every canonical value must pass through coercion unchanged.

    `member_of_session` and the two memory types are absent from
    `_LEGACY_EDGE_TYPE_MAP`; before the by-construction fix they silently
    coerced to `contains`, turning a grouping/memory edge into a false causal
    claim. Parametrising over EdgeType makes the next added type fail here
    rather than silently downgrade in production.
    """
    assert coerce_legacy_edge_type(name) == EdgeType(name)


def test_unknown_edge_type_still_falls_back_to_contains() -> None:
    assert coerce_legacy_edge_type("no_such_edge") == EdgeType.contains
    assert coerce_legacy_edge_type(None) == EdgeType.contains
    assert coerce_legacy_edge_type("produced_by") == EdgeType.contains


# ── Edge construction ─────────────────────────────────────────────────────


@pytest.mark.parametrize("operation", ["write", "update"])
def test_write_emits_run_to_item_edge(operation: str) -> None:
    (edge,) = edges_for_event(_event(operation, "run-a"))
    assert edge.edge_type == "wrote_memory"
    assert edge.source == {"kind": "run", "run_id": "run-a"}
    assert edge.target["kind"] == "memory"
    assert edge.target["memory_key"] == "user_prefs"


def test_read_emits_item_to_run_edge() -> None:
    (edge,) = edges_for_event(_event("read", "run-b"))
    assert edge.edge_type == "read_memory"
    assert edge.source["kind"] == "memory"
    assert edge.target == {"kind": "run", "run_id": "run-b"}


def test_delete_emits_no_edge() -> None:
    assert edges_for_event(_event("delete", "run-c")) == []


def test_edge_carries_event_timestamp_not_wall_clock() -> None:
    edge = edges_for_event(_event("write", "run-a", ts="2020-01-01T00:00:00Z"))[0]
    assert edge.created_at == "2020-01-01T00:00:00Z"


# ── No content capture (ADR-0021 §4) ──────────────────────────────────────


def test_opt_in_value_never_reaches_the_graph() -> None:
    event = _event("write", "run-a", value={"secret": "hunter2"})
    (edge,) = edges_for_event(event)
    assert "hunter2" not in repr(edge.as_dict())


def test_read_records_claimed_origin_as_facet_not_as_source() -> None:
    """The reader's claim about provenance is recorded, not trusted.

    Using the claimed origin as the edge source would let a run assert its
    own provenance; the graph would then confirm whatever the reader said.
    """
    event = _event("read", "run-b", origin_run_id="run-a")
    (edge,) = edges_for_event(event)
    assert edge.source["kind"] == "memory"
    assert edge.facets is not None
    assert edge.facets["claimed_origin_run_id"] == "run-a"


def test_facets_carry_relevance_and_freshness() -> None:
    event = _event("read", "run-b", relevance_score=0.8, freshness_seconds=42.0)
    (edge,) = edges_for_event(event)
    assert edge.facets is not None
    assert edge.facets["relevance_score"] == 0.8
    assert edge.facets["freshness_seconds"] == 42.0


# ── Namespacing ───────────────────────────────────────────────────────────


def test_namespace_separates_identical_keys() -> None:
    a = memory_node("prefs", namespace="agent-1")
    b = memory_node("prefs", namespace="agent-2")
    assert a["node_id"] != b["node_id"]
    assert a["memory_key"] == b["memory_key"] == "prefs"


def test_unnamespaced_node_id_is_stable() -> None:
    assert memory_node("prefs")["node_id"] == memory_node("prefs")["node_id"]
    assert memory_node("prefs")["node_id"] != memory_node("prefs", namespace="a")[
        "node_id"
    ]


# ── Poisoned-read back-trace (NF-114) ─────────────────────────────────────


def test_back_trace_finds_writers_oldest_first() -> None:
    edges = edges_for_events(
        [
            _event("write", "run-good", ts="2026-07-20T10:00:00Z"),
            _event("write", "run-poison", ts="2026-07-20T11:00:00Z"),
            _event("read", "run-victim", ts="2026-07-20T12:00:00Z"),
        ]
    )
    assert writers_of(edges, "user_prefs") == ["run-good", "run-poison"]
    assert readers_of(edges, "user_prefs") == ["run-victim"]


def test_back_trace_is_scoped_to_the_key() -> None:
    edges = edges_for_events(
        [_event("write", "run-a", key="k1"), _event("write", "run-b", key="k2")]
    )
    assert writers_of(edges, "k1") == ["run-a"]
    assert writers_of(edges, "k2") == ["run-b"]
    assert writers_of(edges, "k3") == []


def test_back_trace_respects_namespace() -> None:
    edges = edges_for_events(
        [_event("write", "run-a"), _event("write", "run-b")], namespace="agent-1"
    )
    assert writers_of(edges, "user_prefs", namespace="agent-1") == ["run-a", "run-b"]
    # Without the namespace the caller is asking about a different node.
    assert writers_of(edges, "user_prefs") == []


def test_reader_is_not_reported_as_a_writer() -> None:
    """The direction split is the whole point of two edge types."""
    edges = edges_for_events([_event("read", "run-victim")])
    assert writers_of(edges, "user_prefs") == []
    assert readers_of(edges, "user_prefs") == ["run-victim"]


# ── Additive schema (capsule valid without the facet) ─────────────────────


def test_origin_fields_are_optional() -> None:
    event = _event("read", "run-b")
    assert event.origin_run_id is None
    assert event.origin_memory_key is None
    assert event.origin_timestamp_utc is None


def test_origin_run_id_does_not_default_to_the_reading_run() -> None:
    """Defaulting them equal would make every read look self-sourced."""
    event = _event("read", "run-b")
    assert event.origin_run_id != event.run_id
