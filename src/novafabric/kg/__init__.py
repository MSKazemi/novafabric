"""Capsule Knowledge Graph (KG) — secondary derived artifact alongside per-run lineage.

Aggregates observed entity relationships across capsules (which agents called which
models, which tools were co-invoked) into a queryable KuzuDB graph.

SEPARATE from the lineage KuzuDB instance; default path: $NOVA_DATA_DIR/kg_store/nova_kg.kuzu.

Optional extra required: novafabric[scale-kg]  (kuzu>=0.11.3)
"""
from __future__ import annotations
