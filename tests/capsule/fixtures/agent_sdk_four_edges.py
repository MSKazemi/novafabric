# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Golden fixture: agent SDK producing all four lineage edge types.

Used for blast-radius false-positive rate tests (cap-002, ADR-0044).
"""

from __future__ import annotations

from novafabric.capsule.schema import EdgeType
from novafabric.capsule.ulid_util import new_ulid


def make_four_edge_fixture() -> list[dict]:
    """Return a list of 4 minimal edge dicts — one of each type."""
    global_run_id = new_ulid()
    return [
        {
            "schema_version": "0.2.0",
            "edge_id": new_ulid(),
            "edge_type": EdgeType.contains.value,
            "source_run_id": new_ulid(),
            "target_run_id": new_ulid(),
            "created_at": "2024-01-01T00:00:00+00:00",
            "global_run_id": global_run_id,
        },
        {
            "schema_version": "0.2.0",
            "edge_id": new_ulid(),
            "edge_type": EdgeType.spawned.value,
            "source_run_id": new_ulid(),
            "target_run_id": new_ulid(),
            "created_at": "2024-01-01T00:01:00+00:00",
            "global_run_id": global_run_id,
        },
        {
            "schema_version": "0.2.0",
            "edge_id": new_ulid(),
            "edge_type": EdgeType.delegated_to.value,
            "source_run_id": new_ulid(),
            "target_run_id": new_ulid(),
            "created_at": "2024-01-01T00:02:00+00:00",
            "global_run_id": global_run_id,
            "attributes": {"delegation_reason": "tool_use"},
        },
        {
            "schema_version": "0.2.0",
            "edge_id": new_ulid(),
            "edge_type": EdgeType.replayed_from.value,
            "source_run_id": new_ulid(),
            "target_run_id": new_ulid(),
            "created_at": "2024-01-01T00:03:00+00:00",
            "global_run_id": global_run_id,
        },
    ]
