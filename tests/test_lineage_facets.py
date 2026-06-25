# tests/test_lineage_facets.py
"""Column-level lineage facet tests (ADR-0090 Half 1, gap-003)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.lineage import lineage_app
from novafabric.lineage._facets import (
    COLUMN_FACET_KEY,
    MAX_COLUMNS_PER_FACET,
    ColumnFacet,
    extract_column_facet,
    facets_from_tool_calls,
)
from novafabric.lineage._store import LineageStore, _make_node_from_edge_dict
from novafabric.lineage._types import LineageEdge
from novafabric.lineage._writer import LineageWriter


class TestExtractor:
    def test_select_read_exact(self) -> None:
        facet = extract_column_facet("SELECT name, email FROM customers WHERE id=1")
        assert facet == ColumnFacet(
            table="customers",
            columns=["name", "email"],
            access="read",
            confidence="exact",
        )

    def test_select_star_unresolvable_returns_none(self) -> None:
        assert extract_column_facet("SELECT * FROM customers") is None

    def test_select_with_expression_is_heuristic(self) -> None:
        facet = extract_column_facet("SELECT name, COUNT(id) FROM t GROUP BY name")
        assert facet is not None
        assert facet.columns == ["name"]
        assert facet.confidence == "heuristic"

    def test_insert_write_exact(self) -> None:
        facet = extract_column_facet(
            "INSERT INTO orders (id, total) VALUES (1, 'x')"
        )
        assert facet is not None
        assert (facet.table, facet.access) == ("orders", "write")
        assert facet.columns == ["id", "total"]

    def test_update_write(self) -> None:
        facet = extract_column_facet(
            "UPDATE users SET email = 'a@b.c', active = 1 WHERE id = 2"
        )
        assert facet is not None
        assert facet.columns == ["email", "active"]
        assert facet.access == "write"

    def test_delete_row_level_heuristic(self) -> None:
        facet = extract_column_facet("DELETE FROM sessions WHERE expired = 1")
        assert facet is not None
        assert facet.table == "sessions"
        assert facet.columns == []
        assert facet.confidence == "heuristic"

    def test_no_values_ever_in_output(self) -> None:
        facet = extract_column_facet(
            "UPDATE users SET ssn = '123-45-6789' WHERE name = 'Alice'"
        )
        assert facet is not None
        dumped = facet.model_dump_json()
        assert "123-45-6789" not in dumped
        assert "Alice" not in dumped

    @pytest.mark.parametrize(
        "garbage",
        [
            "",
            "not sql at all",
            "SELECT FROM nothing",
            "SELECT ‮﻿\x00 FROM \n\n",
            "INSERT INTO (no table)",
            "select " + "a," * 200_000,  # ~1MB-ish, over length bound
            "\x00\x01\x02 binary junk",
        ],
    )
    def test_garbage_never_raises(self, garbage: str) -> None:
        assert extract_column_facet(garbage) is None or isinstance(
            extract_column_facet(garbage), ColumnFacet
        )

    def test_column_cap_truncates_and_downgrades(self) -> None:
        cols = ", ".join(f"c{i}" for i in range(MAX_COLUMNS_PER_FACET + 10))
        facet = extract_column_facet(f"SELECT {cols} FROM wide")
        assert facet is not None
        assert len(facet.columns) == MAX_COLUMNS_PER_FACET
        assert facet.confidence == "heuristic"


class TestToolCallExtraction:
    def test_facets_from_tool_calls(self, tmp_path: Path) -> None:
        record = {
            "tool_name": "run_sql",
            "arguments": {"query": "SELECT name, email FROM customers"},
        }
        (tmp_path / "tool-calls.jsonl").write_text(json.dumps(record) + "\n")
        facets = facets_from_tool_calls(tmp_path)
        assert len(facets) == 1
        assert facets[0].table == "customers"

    def test_missing_file_and_bad_json_fail_open(self, tmp_path: Path) -> None:
        assert facets_from_tool_calls(tmp_path) == []
        (tmp_path / "tool-calls.jsonl").write_text("{not json}\n\n[1,2]\n")
        assert facets_from_tool_calls(tmp_path) == []

    def test_duplicate_statements_deduplicated(self, tmp_path: Path) -> None:
        record = {"arguments": {"q": "SELECT a FROM t"}}
        lines = "\n".join(json.dumps(record) for _ in range(3))
        (tmp_path / "tool-calls.jsonl").write_text(lines + "\n")
        assert len(facets_from_tool_calls(tmp_path)) == 1


def _capsule(tmp_path: Path, run_id: str = "run-facet-1") -> Path:
    capsule = tmp_path / run_id
    capsule.mkdir()
    (capsule / "assets.jsonl").write_text(
        json.dumps({"asset_ref": "agent@1.0", "registry": "local"}) + "\n"
    )
    (capsule / "tool-calls.jsonl").write_text(
        json.dumps(
            {"tool_name": "db", "arguments": {"sql": "SELECT name FROM customers"}}
        )
        + "\n"
    )
    return capsule


class TestWriterIntegration:
    def test_infer_attaches_column_facets(self, tmp_path: Path) -> None:
        capsule = _capsule(tmp_path)
        edges = LineageWriter(capsule, "run-facet-1").infer()
        assert edges, "consumed edge expected"
        for edge in edges:
            assert edge.facets is not None
            assert edge.facets[COLUMN_FACET_KEY][0]["table"] == "customers"

    def test_existing_facets_preserved(self, tmp_path: Path) -> None:
        capsule = _capsule(tmp_path)
        (capsule / "assets.jsonl").write_text(
            json.dumps(
                {
                    "asset_ref": "agent@1.0",
                    "registry": "local",
                    "status_at_consumption": "production",
                }
            )
            + "\n"
        )
        edges = LineageWriter(capsule, "run-facet-1").infer()
        assert edges[0].facets is not None
        assert edges[0].facets["status_at_consumption"] == "production"
        assert COLUMN_FACET_KEY in edges[0].facets

    def test_no_sql_means_no_facets_and_no_error(self, tmp_path: Path) -> None:
        capsule = _capsule(tmp_path)
        (capsule / "tool-calls.jsonl").write_text("")
        edges = LineageWriter(capsule, "run-facet-1").infer()
        assert all(
            e.facets is None or COLUMN_FACET_KEY not in e.facets for e in edges
        )


def _store_with_faceted_edge(db_path: Path) -> tuple[LineageStore, LineageEdge]:
    store = LineageStore(db_path=db_path)
    edge = LineageEdge(
        edge_type="consumed",
        source={"kind": "run", "run_id": "run-x"},
        target={"kind": "asset", "asset_ref": "agent@1.0", "registry": "local"},
        confidence="observed",
        capsule_run_id="run-x",
        facets={
            COLUMN_FACET_KEY: [
                ColumnFacet(
                    table="customers",
                    columns=["name"],
                    access="read",
                    confidence="exact",
                ).model_dump()
            ]
        },
    )
    nodes = [
        _make_node_from_edge_dict(edge.source),
        _make_node_from_edge_dict(edge.target),
    ]
    store.replace_capsule_lineage(nodes, [edge], "run-x")
    return store, edge


class TestStoreRoundTrip:
    def test_facets_survive_write_and_query(self, tmp_path: Path) -> None:
        store, edge = _store_with_faceted_edge(tmp_path / "lineage.db")
        results = store.provenance(ref="run-x", kind="run")
        node_ids = [r["node_id"] for r in results]
        start = store._node_id_for_ref("run-x", "run")
        assert start is not None
        payloads = store.edges_for_nodes([start, *node_ids])
        assert payloads[0]["facets"][COLUMN_FACET_KEY] == edge.facets[
            COLUMN_FACET_KEY
        ]

    def test_edges_for_nodes_empty_input(self, tmp_path: Path) -> None:
        store, _ = _store_with_faceted_edge(tmp_path / "lineage.db")
        assert store.edges_for_nodes([]) == []


class TestCli:
    def test_with_facets_prints_columns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, _ = _store_with_faceted_edge(tmp_path / "lineage.db")
        monkeypatch.setattr(
            "novafabric.cli.lineage._get_store", lambda: store
        )
        runner = CliRunner()
        result = runner.invoke(
            lineage_app, ["provenance", "run-x", "--with-facets"]
        )
        assert result.exit_code == 0, result.output
        assert "customers" in result.output
        assert "read/exact" in result.output

    def test_default_output_unchanged_without_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, _ = _store_with_faceted_edge(tmp_path / "lineage.db")
        monkeypatch.setattr(
            "novafabric.cli.lineage._get_store", lambda: store
        )
        runner = CliRunner()
        result = runner.invoke(lineage_app, ["provenance", "run-x"])
        assert result.exit_code == 0
        assert "customers" not in result.output
        assert "Column facets" not in result.output

    def test_blast_radius_with_facets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, _ = _store_with_faceted_edge(tmp_path / "lineage.db")
        monkeypatch.setattr(
            "novafabric.cli.lineage._get_store", lambda: store
        )
        runner = CliRunner()
        result = runner.invoke(
            lineage_app,
            ["blast-radius", "local:agent@1.0", "--kind", "asset", "--with-facets"],
        )
        assert result.exit_code == 0, result.output
        assert "customers" in result.output
