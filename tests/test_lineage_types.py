# tests/test_lineage_types.py
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from novafabric.lineage._types import (
    LineageEdge,
    LineageNode,
    node_id_for,
    node_ref_for_artifact,
    node_ref_for_asset,
    node_ref_for_run,
)


def test_node_id_is_deterministic() -> None:
    id1 = node_id_for("run", "01HXAY7M5JZ8R7K4P9DPBYK2WX")
    id2 = node_id_for("run", "01HXAY7M5JZ8R7K4P9DPBYK2WX")
    assert id1 == id2
    assert len(id1) == 26


def test_node_id_differs_by_kind() -> None:
    assert node_id_for("run", "abc") != node_id_for("asset", "abc")


def test_node_ref_for_run() -> None:
    assert node_ref_for_run("01HXAY7M5JZ8") == "01HXAY7M5JZ8"


def test_node_ref_for_asset_includes_registry() -> None:
    ref = node_ref_for_asset("local", "model:foo@1.0.0+sha256:abc")
    assert ref == "local:model:foo@1.0.0+sha256:abc"


def test_node_ref_for_artifact() -> None:
    ref = node_ref_for_artifact("01RUNID", "outputs/result.txt")
    assert ref == "artifact:01RUNID:outputs/result.txt"


def test_lineage_edge_defaults() -> None:
    edge = LineageEdge(
        edge_type="consumed",
        source={"kind": "run", "run_id": "01RUN"},
        target={"kind": "asset", "asset_ref": "model:foo@1.0.0"},
        confidence="observed",
        capsule_run_id="01RUN",
    )
    assert edge.schema_version == "0.1.0"
    assert edge.direction == "source_to_target"
    assert len(edge.edge_id) > 0
    assert edge.emitter["name"] == "novafabric"


def test_lineage_node_fields() -> None:
    node = LineageNode(
        node_id=node_id_for("run", "01RUN"),
        kind="run",
        ref="01RUN",
        first_seen_capsule_run_id="01RUN",
        payload={"kind": "run", "run_id": "01RUN"},
    )
    assert node.kind == "run"
    assert len(node.node_id) == 26


def test_lineage_edge_schema_validates_consumed_record() -> None:
    schema_path = Path("src/novafabric/schemas/lineage-edge.schema.json")
    schema = json.loads(schema_path.read_text())
    record = {
        "schema_version": "0.1.0",
        "edge_id": "01KR37TZR0G3S3FFX5V7KFJ4TA",
        "edge_type": "consumed",
        "source": {"kind": "run", "run_id": "01KR37TZR0G3S3FFX5V7KFJ4TB"},
        "target": {"kind": "asset", "asset_ref": "model:foo@1.0.0", "registry": "local"},
        "direction": "source_to_target",
        "created_at": "2026-05-08T10:00:00.000Z",
        "emitter": {"name": "novafabric", "version": "0.4.0"},
        "confidence": "observed",
    }
    jsonschema.validate(record, schema)  # raises if invalid


def test_lineage_edge_schema_rejects_unknown_edge_type() -> None:
    schema_path = Path("src/novafabric/schemas/lineage-edge.schema.json")
    schema = json.loads(schema_path.read_text())
    record = {
        "schema_version": "0.1.0",
        "edge_id": "01KR37TZR0G3S3FFX5V7KFJ4TC",
        "edge_type": "unknown_type",
        "source": {"kind": "run", "run_id": "01KR37TZR0G3S3FFX5V7KFJ4TB"},
        "target": {"kind": "asset", "asset_ref": "model:foo@1.0.0", "registry": "local"},
        "direction": "source_to_target",
        "created_at": "2026-05-08T10:00:00.000Z",
        "emitter": {"name": "novafabric", "version": "0.4.0"},
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(record, schema)
