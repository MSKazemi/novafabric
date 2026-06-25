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
"""W3C PROV-JSON export for NovaFabric Run Capsules.

Generates W3C PROV-JSON (https://www.w3.org/TR/prov-json/) from the
lineage.jsonl file inside a capsule directory.

Each lineage edge maps to a ``wasDerivedFrom`` or ``used`` relation.
Each run_id becomes an ``activity``.
Each capsule (source/target) becomes an ``entity``.

No external PROV library is required — the JSON is hand-crafted per the spec.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Namespace prefix used for all NovaFabric identifiers
_NF_PREFIX = "nf"
_NF_URI = "https://novafabric.io/terms/"

# W3C PROV namespace
_PROV_PREFIX = "prov"
_PROV_URI = "http://www.w3.org/ns/prov#"

# Edge type → PROV relation mapping
_EDGE_TYPE_TO_PROV: dict[str, str] = {
    "derived": "wasDerivedFrom",
    "used": "used",
    "generated": "wasGeneratedBy",
    "influenced": "wasInfluencedBy",
    # Default fallback
    "": "wasDerivedFrom",
}


def _prov_id(prefix: str, local: str) -> str:
    """Build a prefixed PROV identifier string."""
    return f"{prefix}:{local}"


def _sanitize_id(value: str) -> str:
    """Replace characters illegal in PROV IDs with underscores."""
    import re
    return re.sub(r"[^A-Za-z0-9_\-.]", "_", value)


def _node_id(value: object) -> str | None:
    """Normalize a lineage node reference to a stable string id.

    Lineage edges (schema v0.1.0) carry structured ``source``/``target`` nodes —
    e.g. ``{"kind": "run", "run_id": "..."}`` or
    ``{"kind": "asset", "asset_ref": "...@v1"}`` — not bare strings. Older/flat
    schemas used string ids directly. Accept both so PROV-JSON export does not
    crash on real capsules.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        kind = value.get("kind")
        for key in ("run_id", "asset_ref", "id", "ref", "name", "uri"):
            ident = value.get(key)
            if ident:
                return f"{kind}:{ident}" if kind else str(ident)
        return str(kind) if kind else None
    return str(value)


def export_prov_json(capsule_dir: Path) -> dict[str, Any]:
    """Generate a W3C PROV-JSON document from ``lineage.jsonl``.

    Args:
        capsule_dir: Path to the capsule directory.  ``lineage.jsonl`` is read
            from this directory.  If the file is absent or empty, a valid
            skeleton document is returned.

    Returns:
        A ``dict`` conforming to the W3C PROV-JSON schema with keys:
        ``prefix``, ``entity``, ``activity``, ``wasGeneratedBy``,
        ``used``, ``wasDerivedFrom``.

    Raises:
        FileNotFoundError: If ``capsule_dir`` does not exist.
    """
    if not capsule_dir.exists():
        raise FileNotFoundError(f"Capsule directory not found: {capsule_dir}")

    # PROV-JSON skeleton
    doc: dict[str, Any] = {
        "prefix": {
            _PROV_PREFIX: _PROV_URI,
            _NF_PREFIX: _NF_URI,
        },
        "entity": {},
        "activity": {},
        "wasGeneratedBy": {},
        "used": {},
        "wasDerivedFrom": {},
    }

    lineage_path = capsule_dir / "lineage.jsonl"
    if not lineage_path.exists():
        return doc

    edges = []
    for line in lineage_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                edges.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    if not edges:
        return doc

    relation_counters: dict[str, int] = {}

    for edge in edges:
        # Extract node IDs from common lineage edge schemas
        from_id: str | None = _node_id(
            edge.get("from_id")
            or edge.get("source_id")
            or edge.get("source")
            or edge.get("from")
        )
        to_id: str | None = _node_id(
            edge.get("to_id")
            or edge.get("target_id")
            or edge.get("target")
            or edge.get("to")
        )
        run_id: str | None = (
            edge.get("run_id")
            or edge.get("capsule_run_id")
            or _node_id(edge.get("source"))
        )
        edge_type: str = edge.get("edge_type") or edge.get("type") or ""
        timestamp: str | None = edge.get("timestamp") or edge.get("created_at")

        # Register entities
        if from_id:
            safe = _sanitize_id(from_id)
            eid = _prov_id(_NF_PREFIX, safe)
            if eid not in doc["entity"]:
                doc["entity"][eid] = {"prov:type": _prov_id(_NF_PREFIX, "Capsule")}

        if to_id:
            safe = _sanitize_id(to_id)
            eid = _prov_id(_NF_PREFIX, safe)
            if eid not in doc["entity"]:
                doc["entity"][eid] = {"prov:type": _prov_id(_NF_PREFIX, "Capsule")}

        # Register activity from run_id
        if run_id:
            safe = _sanitize_id(run_id)
            aid = _prov_id(_NF_PREFIX, safe)
            if aid not in doc["activity"]:
                activity_entry: dict[str, Any] = {
                    "prov:type": _prov_id(_NF_PREFIX, "Run"),
                }
                if timestamp:
                    activity_entry["prov:startTime"] = timestamp
                doc["activity"][aid] = activity_entry

        # Build relation
        if not from_id or not to_id:
            continue

        prov_relation = _EDGE_TYPE_TO_PROV.get(edge_type, "wasDerivedFrom")
        relation_key = prov_relation

        # Generate a unique relation ID
        relation_counters[relation_key] = relation_counters.get(relation_key, 0) + 1
        rid = _prov_id(_NF_PREFIX, f"{relation_key}_{relation_counters[relation_key]}")

        from_eid = _prov_id(_NF_PREFIX, _sanitize_id(from_id))
        to_eid = _prov_id(_NF_PREFIX, _sanitize_id(to_id))

        if prov_relation == "wasGeneratedBy":
            # wasGeneratedBy: entity was generated by activity
            doc["wasGeneratedBy"][rid] = {
                "prov:entity": to_eid,
                "prov:activity": from_eid,
            }
        elif prov_relation == "used":
            doc["used"][rid] = {
                "prov:activity": (
                    _prov_id(_NF_PREFIX, _sanitize_id(run_id)) if run_id else from_eid
                ),
                "prov:entity": from_eid,
            }
        else:
            # wasDerivedFrom (default) and wasInfluencedBy
            doc["wasDerivedFrom"][rid] = {
                "prov:generatedEntity": to_eid,
                "prov:usedEntity": from_eid,
            }

    return doc
