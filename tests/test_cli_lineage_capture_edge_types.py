"""M15: ``nova lineage --edge-type`` accepts every edge type the store can hold.

``nova capture`` writes ``consumed`` (run -> asset) and ``produced_by`` (artifact -> run)
edges, but the CLI's hand-kept allow-list named only the typed ``EdgeType`` vocabulary,
so ``--edge-type consumed`` exited 1 with "Unknown edge types" for the edges capture
actually writes. The allowed set is now derived from the edge-type vocabularies
themselves. The CLI's own help example (``nova lineage blast-radius my-agent@v1.0``)
also never resolved: asset nodes are stored as ``<registry>:<name>@<version>``.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import novafabric.cli.lineage as lineage_mod
from novafabric.capsule.schema import EdgeType
from novafabric.cli.lineage import lineage_app
from novafabric.lineage._store import LineageStore
from novafabric.lineage._types import (
    LINEAGE_EDGE_TYPES,
    LineageEdge,
    LineageNode,
    node_id_for,
    node_ref_for_artifact,
    node_ref_for_asset,
)

runner = CliRunner()

RUN = "01HZZZZZZZZZZZZZZZZZZZZZZZ"
ASSET = node_ref_for_asset("local", "my-agent@v1.0")
ARTIFACT = node_ref_for_artifact(RUN, "outputs/answer.txt")


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LineageStore:
    db_path = tmp_path / "lineage.db"
    seeded = LineageStore(db_path=db_path)
    nodes = [
        LineageNode(node_id_for("run", RUN), "run", RUN, RUN, {"kind": "run", "run_id": RUN}),
        LineageNode(
            node_id_for("asset", ASSET), "asset", ASSET, RUN,
            {"kind": "asset", "asset_ref": "my-agent@v1.0", "registry": "local"},
        ),
        LineageNode(
            node_id_for("artifact", ARTIFACT), "artifact", ARTIFACT, RUN,
            {"kind": "artifact", "artifact_ref": {"capsule_run_id": RUN, "path": "outputs/answer.txt"}},
        ),
    ]
    edges = [
        LineageEdge(
            edge_type="consumed",
            source={"kind": "run", "run_id": RUN},
            target={"kind": "asset", "asset_ref": "my-agent@v1.0", "registry": "local"},
            capsule_run_id=RUN,
            confidence="observed",
        ),
        LineageEdge(
            edge_type="produced_by",
            source={"kind": "artifact", "artifact_ref": {"capsule_run_id": RUN, "path": "outputs/answer.txt"}},
            target={"kind": "run", "run_id": RUN},
            capsule_run_id=RUN,
            confidence="inferred",
        ),
    ]
    seeded.replace_capsule_lineage(nodes, edges, RUN)
    monkeypatch.setattr(lineage_mod, "_get_store", lambda: LineageStore(db_path=db_path))
    return seeded


def _json(args: list[str]) -> list[dict[str, object]]:
    result = runner.invoke(lineage_app, args)
    assert result.exit_code == 0, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    return json.loads(result.output)


def test_provenance_filters_on_consumed(store: LineageStore) -> None:
    rows = _json(["provenance", RUN, "--edge-type", "consumed", "--output", "json"])
    assert {r["ref"] for r in rows} == {ASSET}


def test_blast_radius_filters_on_produced_by(store: LineageStore) -> None:
    rows = _json(["blast-radius", RUN, "--edge-type", "produced_by", "--output", "json"])
    assert {r["ref"] for r in rows} == {ARTIFACT}


def test_bare_asset_ref_resolves_to_the_local_registry(store: LineageStore) -> None:
    """The help example ``nova lineage blast-radius my-agent@v1.0`` works."""
    rows = _json(["blast-radius", "my-agent@v1.0", "--output", "json"])
    assert {r["ref"] for r in rows} == {RUN, ARTIFACT}


def test_unknown_bare_asset_ref_names_the_registry_form(store: LineageStore) -> None:
    result = runner.invoke(lineage_app, ["blast-radius", "other@v2"])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "Unknown ref: other@v2" in result.output
    assert "local:other@v2" in result.output


def test_unknown_edge_type_lists_capture_edges(store: LineageStore) -> None:
    result = runner.invoke(lineage_app, ["provenance", RUN, "--edge-type", "bogus"])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "consumed" in result.output and "produced_by" in result.output


def _packaged_schema_enum() -> set[str]:
    import novafabric

    schema = Path(novafabric.__file__).parent / "schemas" / "lineage-edge.schema.json"
    return set(json.loads(schema.read_text())["properties"]["edge_type"]["enum"])


def _writer_literals() -> set[str]:
    """Every ``edge_type="<literal>"`` the lineage writers emit."""
    import novafabric.lineage._writer as writer
    import novafabric.lineage.memory as memory

    found: set[str] = set()
    for module in (writer, memory):
        tree = ast.parse(Path(module.__file__ or "").read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.keyword)
                and node.arg == "edge_type"
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                found.add(node.value.value)
    return found


def test_lineage_edge_types_cover_every_vocabulary() -> None:
    """One property over all N sources: the in-force schema, the typed enum, the writers."""
    assert _packaged_schema_enum() <= LINEAGE_EDGE_TYPES
    assert {e.value for e in EdgeType} <= LINEAGE_EDGE_TYPES
    writers = _writer_literals()
    assert {"consumed", "produced_by", "replayed_from"} <= writers
    assert writers <= LINEAGE_EDGE_TYPES


def test_cli_accepts_every_lineage_edge_type(store: LineageStore) -> None:
    for edge_type in sorted(LINEAGE_EDGE_TYPES):
        result = runner.invoke(lineage_app, ["provenance", RUN, "--edge-type", edge_type])
        assert result.exit_code == 0, (edge_type, result.output)


def test_help_lists_capture_edge_types() -> None:
    result = runner.invoke(lineage_app, ["provenance", "--help"], env={"COLUMNS": "400"})
    assert result.exit_code == 0
    assert "consumed" in result.output
    assert "produced_by" in result.output
