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

"""Edge typer — pure function that resolves default edge_type from role-pair (cap-002).

FR-07: Every lineage-edge record MUST carry an `edge_type` field.
Pre-0.2.0 schemaless rows are interpreted as `contains` for backward compat.

Resolution rules (role-pair → default edge_type):
  (PARENT, CHILD)        → contains      (parent contains its workers)
  (DRIVER, WORKER)       → contains      (driver spawns workers, structural)
  (STANDALONE, CHILD)    → spawned       (standalone spawned a sub-task)
  (any, replay_target)   → replayed_from (replay run ← original run)
  (CHILD, DELEGATED)     → delegated_to  (child delegated subtask)
  default                → contains
"""

from __future__ import annotations

from novafabric.capsule.schema import CapsuleRole, DistributionRole, EdgeType

# Role-pair lookup table: (source_capsule_role, target_capsule_role) → EdgeType
_ROLE_PAIR_MAP: dict[tuple[str, str], EdgeType] = {
    # Structural containment (parent/child hierarchy)
    (CapsuleRole.PARENT.value, CapsuleRole.CHILD.value): EdgeType.contains,
    (CapsuleRole.CHILD.value, CapsuleRole.CHILD.value): EdgeType.contains,
    # Driver → worker containment (cap-006 DDP scenario)
    (DistributionRole.DRIVER.value, DistributionRole.WORKER.value): EdgeType.contains,
    # Standalone spawning a sub-task
    (CapsuleRole.STANDALONE.value, CapsuleRole.CHILD.value): EdgeType.spawned,
    # Replay relationship
    ("replay_source", "replay_target"): EdgeType.replayed_from,
    # Delegation
    (CapsuleRole.CHILD.value, "delegated"): EdgeType.delegated_to,
}

_LEGACY_EDGE_TYPE_MAP: dict[str, EdgeType] = {
    # Pre-0.2.0 edge_type strings → typed EdgeType (FR-07 backward compat)
    "consumed": EdgeType.contains,
    "produced_by": EdgeType.contains,
    "derived_from": EdgeType.spawned,
    "evaluated_by": EdgeType.spawned,
    "attested_by": EdgeType.contains,
    "replayed_from": EdgeType.replayed_from,
    # Already typed:
    "contains": EdgeType.contains,
    "spawned": EdgeType.spawned,
    "delegated_to": EdgeType.delegated_to,
}


def resolve_edge_type(
    source_role: str | None = None,
    target_role: str | None = None,
    *,
    is_replay: bool = False,
    is_delegation: bool = False,
) -> EdgeType:
    """Resolve the default edge_type from role context.

    Args:
        source_role: CapsuleRole or DistributionRole value of the source.
        target_role: CapsuleRole or DistributionRole value of the target.
        is_replay:   True when this edge represents a replay relationship.
        is_delegation: True when source delegates to target.

    Returns:
        The default EdgeType for this role pair.
    """
    if is_replay:
        return EdgeType.replayed_from
    if is_delegation:
        return EdgeType.delegated_to

    if source_role is not None and target_role is not None:
        key = (source_role, target_role)
        if key in _ROLE_PAIR_MAP:
            return _ROLE_PAIR_MAP[key]

    # Default fallback — structural containment
    return EdgeType.contains


def coerce_legacy_edge_type(raw: str | None) -> EdgeType:
    """Coerce a pre-0.2.0 (schemaless) edge_type string to the typed enum.

    Per FR-07: pre-0.2.0 rows without edge_type are interpreted as 'contains'.
    Returns EdgeType.contains for None or unknown values.
    """
    if raw is None:
        return EdgeType.contains
    mapped = _LEGACY_EDGE_TYPE_MAP.get(raw)
    if mapped is not None:
        return mapped
    # Unknown legacy string — conservative fallback
    return EdgeType.contains
