"""An artifact endpoint without its own ``capsule_run_id`` imports (found while fixing serve-06).

The importer derived an artifact node's id with the capsule's run id as fallback;
the store derived the edge's endpoints with ``""`` as fallback
(``lineage._types.node_ref_from_edge_dict``). The edge therefore referenced a node
that was never inserted, and every such edge failed with ``FOREIGN KEY constraint
failed`` — the whole capsule's lineage import with it.
"""
from __future__ import annotations

import json
from pathlib import Path

from novafabric.lineage._importer import index_capsule_lineage


def _edge(artifact_ref: dict[str, str]) -> dict[str, object]:
    return {
        "edge_id": "e-art",
        "edge_type": "produced_by",
        "source": {"kind": "artifact", "artifact_ref": artifact_ref},
        "target": {"kind": "run", "run_id": "01RUNART"},
        "direction": "source_to_target",
        "emitter": {"name": "other-tool", "version": "1"},
        "capsule_run_id": "01RUNART",
        "schema_version": "0.1.0",
        "created_at": "2026-10-24T00:00:00Z",
    }


def _index(tmp_path: Path, artifact_ref: dict[str, str]) -> tuple[int, Path]:
    cap = tmp_path / "01RUNART"
    cap.mkdir()
    (cap / "lineage.jsonl").write_text(json.dumps(_edge(artifact_ref)) + "\n")
    db = tmp_path / "lineage.db"
    return index_capsule_lineage(cap, db_path=db).edges_indexed, db


def test_an_artifact_without_its_own_capsule_run_id_imports(tmp_path: Path) -> None:
    indexed, _ = _index(tmp_path, {"path": "outputs/report.txt"})
    assert indexed == 1


def test_the_imported_artifact_belongs_to_the_capsule(tmp_path: Path) -> None:
    import sqlite3

    _, db = _index(tmp_path, {"path": "outputs/report.txt"})
    with sqlite3.connect(db) as conn:
        refs = [r[0] for r in conn.execute("SELECT ref FROM lineage_nodes WHERE kind='artifact'")]
    assert refs == ["artifact:01RUNART:outputs/report.txt"]


def test_an_explicit_capsule_run_id_is_kept(tmp_path: Path) -> None:
    import sqlite3

    _, db = _index(tmp_path, {"capsule_run_id": "01OTHER", "path": "a.bin"})
    with sqlite3.connect(db) as conn:
        refs = [r[0] for r in conn.execute("SELECT ref FROM lineage_nodes WHERE kind='artifact'")]
    assert refs == ["artifact:01OTHER:a.bin"]
