"""Write a tiny capsule whose lineage is mostly benign with one planted attack edge.

Produces ``examples/spkg-anomaly-scan/capsule/lineage.jsonl`` — the input for
``nova kg detect``. No NovaFabric import needed; the file is plain JSONL in the same
shape the lineage importer consumes (``edge_type`` + ``source``/``target`` nodes).
"""
from __future__ import annotations

import json
from pathlib import Path

CAPSULE = Path(__file__).parent / "capsule"
RUN = "run-demo"
TS = "2026-07-02T14:00:00.000000Z"


def _edge(edge_type: str, s_kind: str, s_ref: str, t_kind: str, t_ref: str) -> dict:
    return {
        "edge_type": edge_type,
        "source": {"kind": s_kind, "ref": s_ref},
        "target": {"kind": t_kind, "ref": t_ref},
        "created_at": TS,
        "capsule_run_id": RUN,
    }


def main() -> None:
    edges = [
        # 20 benign edges: the fleet's "normal" — runs read the training set,
        # write a model artifact, call the same well-known tool.
        *[
            _edge("uses", "run", f"run-{i}", "dataset", "dataset:training-set")
            for i in range(20)
        ],
        *[
            _edge("produces", "run", f"run-{i}", "artifact", f"artifact:model-{i}.pkl")
            for i in range(20)
        ],
        # 1 planted attack edge: a run executes a shell — never seen in the benign set.
        _edge("executes", "run", "run-evil", "tool", "tool:/bin/shell"),
    ]
    CAPSULE.mkdir(parents=True, exist_ok=True)
    (CAPSULE / "lineage.jsonl").write_text(
        "\n".join(json.dumps(e) for e in edges) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(edges)} edges to {CAPSULE / 'lineage.jsonl'}")


if __name__ == "__main__":
    main()
