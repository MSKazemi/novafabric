"""Sovereignty static-analysis tests — belt-and-suspenders FR-10 guard (C-6).

These tests parse the source of shard_query.py and coordinator.py with ast.parse
and assert that raw LineageEdge field names never appear in response payloads.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Source files under scrutiny
_SRC_ROOT = (
    Path(__file__).resolve().parent.parent.parent
    / "src" / "novafabric" / "lineage" / "federation"
)
SHARD_QUERY_PATH = _SRC_ROOT / "shard_query.py"
COORDINATOR_PATH = _SRC_ROOT / "coordinator.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _response_model_fields(source: str, model_name: str) -> list[str]:
    """Return field names declared in a Pydantic BaseModel class."""
    tree = ast.parse(source)
    fields: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == model_name:
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    fields.append(stmt.target.id)
    return fields


def _dict_literal_keys(source: str) -> list[str]:
    """Return all string keys from dict literals in *source*."""
    tree = ast.parse(source)
    keys: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.append(k.value)
    return keys


# ---------------------------------------------------------------------------
# C-6 tests
# ---------------------------------------------------------------------------


def test_shard_query_source_files_exist() -> None:
    assert SHARD_QUERY_PATH.exists(), f"Missing: {SHARD_QUERY_PATH}"
    assert COORDINATOR_PATH.exists(), f"Missing: {COORDINATOR_PATH}"


def test_ShardLocalResponse_has_no_edges_field() -> None:
    source = _load_source(SHARD_QUERY_PATH)
    fields = _response_model_fields(source, "ShardLocalResponse")
    assert "edges" not in fields, (
        f"ShardLocalResponse must not have an 'edges' field; found fields: {fields}"
    )


def test_ShardLocalResponse_has_rows_field() -> None:
    source = _load_source(SHARD_QUERY_PATH)
    fields = _response_model_fields(source, "ShardLocalResponse")
    assert "rows" in fields, (
        f"ShardLocalResponse must have a 'rows' field; found: {fields}"
    )


def test_LineageEdge_not_in_shard_query_response_annotations() -> None:
    """LineageEdge must not appear in any response type annotation in shard_query.py."""
    source = _load_source(SHARD_QUERY_PATH)
    tree = ast.parse(source)
    # Find all ClassDef nodes whose name ends with 'Response'
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and "Response" in node.name:
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign):
                    annotation_src = ast.unparse(stmt.annotation)
                    assert "LineageEdge" not in annotation_src, (
                        f"Response model '{node.name}' field annotation contains "
                        f"'LineageEdge': {annotation_src}"
                    )


def test_shard_query_dict_literals_do_not_include_raw_edge_keys() -> None:
    """Dict literals in shard_query.py response construction must not include edge keys."""
    source = _load_source(SHARD_QUERY_PATH)
    # _to_node_row is the projection function; its output must not contain edge fields
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_to_node_row":
            fn_source = ast.unparse(node)
            assert "source_run_id" not in fn_source, (
                "_to_node_row must not include 'source_run_id'"
            )
            assert "target_run_id" not in fn_source, (
                "_to_node_row must not include 'target_run_id'"
            )


def test_coordinator_response_does_not_reference_LineageEdge() -> None:
    """FederationResponse in coordinator.py must not reference LineageEdge."""
    source = _load_source(COORDINATOR_PATH)
    fields = _response_model_fields(source, "FederationResponse")
    assert "edges" not in fields, (
        f"FederationResponse must not have 'edges' field; found: {fields}"
    )


def test_shard_query_row_dicts_lack_raw_edge_fields_at_runtime() -> None:
    """Integration: actual rows returned from shard endpoint lack edge fields."""
    import tempfile

    from fastapi.testclient import TestClient

    from novafabric.lineage._types import LineageEdge
    from novafabric.lineage.backends.sqlite import SqliteLineageStore
    from novafabric.lineage.federation.shard_query import create_shard_app

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "lin.db"
        store = SqliteLineageStore(db_path=db_path, site_id="site-x")
        store.insert(
            LineageEdge(
                edge_type="contains",
                source={"kind": "run", "run_id": "run-X1"},
                target={"kind": "run", "run_id": "run-X2"},
                confidence="high",
                capsule_run_id="cap-X",
            )
        )
        app = create_shard_app(store=store, site_id="site-x")
        client = TestClient(app)
        resp = client.post(
            "/lineage/shard-local-query",
            json={
                "query_type": "provenance",
                "root_run_id": "run-X1",
                "max_depth": 5,
                "site_id": "coordinator",
            },
        )
        assert resp.status_code == 200
        rows = resp.json()["rows"]
        for row in rows:
            assert "source_run_id" not in row, f"Sovereignty violation: {row}"
            assert "target_run_id" not in row, f"Sovereignty violation: {row}"
