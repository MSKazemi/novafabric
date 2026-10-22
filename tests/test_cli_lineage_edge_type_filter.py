"""``nova lineage provenance|blast-radius --edge-type`` restricts the traversal.

Regression: the CLI filtered the traversal's *result rows* on
``row["edge_type"]``, but those rows are nodes (``node_id/kind/ref/payload``)
and never carry an edge type, so any ``--edge-type`` returned ``[]``. The
existing acceptance tests asserted only ``exit_code == 0`` and could not see it.

The filter now runs inside the traversal (``LineageStore``): only edges whose
type is in the requested set are walked. Seeded graph, three edge types::

    A --spawned--> B --delegated_to--> D
    A --contains--> C
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import novafabric.cli.lineage as lineage_mod
from novafabric.cli.lineage import lineage_app
from novafabric.lineage._store import LineageStore
from novafabric.lineage._types import LineageEdge, LineageNode, node_id_for

runner = CliRunner()

A, B, C, D = "run-edge-a", "run-edge-b", "run-edge-c", "run-edge-d"


def _node(ref: str) -> LineageNode:
    return LineageNode(
        node_id=node_id_for("run", ref),
        kind="run",
        ref=ref,
        first_seen_capsule_run_id=None,
        payload={"kind": "run", "run_id": ref},
    )


def _edge(edge_type: str, src: str, dst: str) -> LineageEdge:
    return LineageEdge(
        edge_type=edge_type,
        source={"kind": "run", "run_id": src},
        target={"kind": "run", "run_id": dst},
        capsule_run_id=A,
        confidence="high",
    )


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LineageStore:
    db_path = tmp_path / "lineage.db"
    seeded = LineageStore(db_path=db_path)
    seeded.replace_capsule_lineage(
        [_node(r) for r in (A, B, C, D)],
        [_edge("spawned", A, B), _edge("contains", A, C), _edge("delegated_to", B, D)],
        A,
    )
    monkeypatch.setattr(lineage_mod, "_get_store", lambda: LineageStore(db_path=db_path))
    return seeded


def _refs(command: str, ref: str, *extra: str) -> set[str]:
    result = runner.invoke(lineage_app, [command, ref, "--output", "json", *extra])
    assert result.exit_code == 0, result.output
    assert isinstance(result.exception, (SystemExit, type(None))), result.exception
    return {row["ref"] for row in json.loads(result.output)}


# ── provenance (walks source -> target) ─────────────────────────────────────


@pytest.mark.parametrize(
    ("edge_type", "expected"),
    [
        (None, {B, C, D}),
        ("spawned", {B}),
        ("contains", {C}),
        ("spawned,delegated_to", {B, D}),
        ("contains,spawned", {B, C}),
        (" spawned , contains ", {B, C}),
        ("delegated_to", set()),  # A has no delegated_to edge, so nothing is reachable
    ],
)
def test_provenance_edge_type_filter(
    store: LineageStore, edge_type: str | None, expected: set[str]
) -> None:
    extra = ["--edge-type", edge_type] if edge_type is not None else []
    assert _refs("provenance", A, *extra) == expected


# ── blast-radius (walks target -> source) ───────────────────────────────────


@pytest.mark.parametrize(
    ("edge_type", "expected"),
    [
        (None, {A, B}),
        ("delegated_to", {B}),
        ("delegated_to,spawned", {A, B}),
        ("contains", set()),
    ],
)
def test_blast_radius_edge_type_filter(
    store: LineageStore, edge_type: str | None, expected: set[str]
) -> None:
    extra = ["--edge-type", edge_type] if edge_type is not None else []
    assert _refs("blast-radius", D, *extra) == expected


def test_filtered_to_nothing_on_a_known_ref_is_not_unknown_ref(store: LineageStore) -> None:
    """An empty filtered result for a ref that exists exits 0, not 'Unknown ref'."""
    result = runner.invoke(lineage_app, ["blast-radius", D, "--edge-type", "contains"])
    assert result.exit_code == 0, result.output
    assert "Unknown ref" not in result.output


def test_text_output_lists_only_filtered_nodes(store: LineageStore) -> None:
    result = runner.invoke(lineage_app, ["provenance", A, "--edge-type", "contains"])
    assert result.exit_code == 0, result.output
    assert C in result.output
    assert B not in result.output


def test_unknown_edge_type_still_exits_1(store: LineageStore) -> None:
    result = runner.invoke(lineage_app, ["provenance", A, "--edge-type", "spawned,bogus"])
    assert result.exit_code == 1
    assert "Unknown edge types: bogus" in result.output


# ── store layer ─────────────────────────────────────────────────────────────


def test_store_accepts_a_set_of_edge_types(store: LineageStore) -> None:
    refs = {r["ref"] for r in store.provenance(A, kind="run", edge_type={"spawned", "delegated_to"})}
    assert refs == {B, D}


def test_store_single_edge_type_string_unchanged(store: LineageStore) -> None:
    """The pre-existing single-string form keeps its behaviour (diagnose/attribution uses it)."""
    assert {r["ref"] for r in store.provenance(A, kind="run", edge_type="contains")} == {C}
    assert {r["ref"] for r in store.blast_radius(D, kind="run", edge_type="delegated_to")} == {B}


def test_store_empty_edge_type_set_means_no_filter(store: LineageStore) -> None:
    assert {r["ref"] for r in store.provenance(A, kind="run", edge_type=set())} == {B, C, D}
