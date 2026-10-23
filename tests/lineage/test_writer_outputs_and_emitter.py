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
"""L14: manifest ``outputs`` entries are Artifact objects; L7: edges name the real emitter.

The run-capsule schema makes every ``outputs[]`` item an Artifact object
(``name``, ``path``, ``content_hash``, …) and ``nova capture`` writes one per
stored media blob (ADR-0125). The writer iterated ``outputs`` as path strings,
so the whole dict became the artifact ``path`` and the node ref read
``artifact:<run>:{'name': …}``. Legacy string entries keep working.

Every edge recorded ``emitter.version: "0.4.0"`` whatever NovaFabric wrote it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import novafabric
from novafabric.lineage._types import LineageEdge, node_from_edge_dict
from novafabric.lineage._writer import LineageWriter

RUN = "01HXRUN000000000000001"
HASH = "sha256:" + "ab" * 32
MEDIA = {
    "name": "image-abababababab",
    "path": "media/sha256/ab/abab.png",
    "content_hash": HASH,
    "size_bytes": 12,
    "media_type": "image/png",
}


def _capsule(tmp_path: Path, outputs: list[Any]) -> Path:
    cap = tmp_path / RUN
    cap.mkdir()
    (cap / "capsule.yaml").write_text(yaml.dump({"run_id": RUN, "outputs": outputs}))
    return cap


def _produced(cap: Path) -> list[LineageEdge]:
    edges = LineageWriter(capsule_dir=cap, run_id=RUN).infer()
    return [e for e in edges if e.edge_type == "produced_by"]


def test_an_artifact_object_output_becomes_a_node_keyed_by_its_path(tmp_path: Path) -> None:
    [edge] = _produced(_capsule(tmp_path, [MEDIA]))
    ref = edge.source["artifact_ref"]
    assert ref["path"] == MEDIA["path"]
    assert ref["capsule_run_id"] == RUN
    # Additive identity hints carried from the Artifact (the node key stays the path).
    assert ref["name"] == MEDIA["name"]
    assert ref["content_hash"] == HASH
    node = node_from_edge_dict(edge.source)
    assert node.ref == f"artifact:{RUN}:{MEDIA['path']}"
    assert "{" not in node.ref


def test_a_legacy_string_output_still_works(tmp_path: Path) -> None:
    [edge] = _produced(_capsule(tmp_path, ["outputs/report.txt"]))
    assert edge.source["artifact_ref"] == {"capsule_run_id": RUN, "path": "outputs/report.txt"}


def test_mixed_and_malformed_outputs(tmp_path: Path) -> None:
    """Strings and Artifacts both map; entries with no usable path are skipped, not stringified."""
    inline = {"name": "final-response", "path": "", "content_hash": HASH, "size_bytes": 2,
              "media_type": "text/plain", "inline": True, "inline_value": "hi"}
    outputs = ["outputs/a.txt", MEDIA, inline, 42, {"name": "no-path"}, "", None]
    paths = [e.source["artifact_ref"]["path"] for e in _produced(_capsule(tmp_path, outputs))]
    assert paths == ["outputs/a.txt", MEDIA["path"], "inline:final-response"]


def test_a_non_list_outputs_field_yields_no_edges(tmp_path: Path) -> None:
    cap = tmp_path / RUN
    cap.mkdir()
    (cap / "capsule.yaml").write_text(yaml.dump({"run_id": RUN, "outputs": {"x": 1}}))
    assert _produced(cap) == []


@pytest.mark.parametrize("producer", ["writer", "dataclass"])
def test_edges_record_the_installed_novafabric_version(tmp_path: Path, producer: str) -> None:
    if producer == "writer":
        [edge] = _produced(_capsule(tmp_path, ["outputs/a.txt"]))
    else:
        edge = LineageEdge(
            edge_type="consumed", source={"kind": "run", "run_id": RUN},
            target={"kind": "asset", "asset_ref": "a", "registry": "local"},
            confidence="observed", capsule_run_id=RUN,
        )
    assert edge.emitter == {"name": "novafabric", "version": novafabric.__version__}
    assert edge.emitter["version"] != "0.4.0"
