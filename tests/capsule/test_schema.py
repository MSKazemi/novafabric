# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for Pydantic v2 models and JSON Schema validation (FR-01, FR-07, FR-21, FR-25)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from novafabric.capsule.schema import (
    CapsuleRole,
    ChildCapsule,
    DistributionRole,
    EdgeType,
    FailMode,
    LineageEdgeV2,
    OrphanPlaceholder,
    ParentCapsule,
    ParentStatus,
)
from novafabric.capsule.ulid_util import new_ulid

SCHEMA_PATH = Path(__file__).parents[2] / "schemas" / "parent_child_capsule_v1.schema.json"


@pytest.fixture
def json_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


# ── Schema artifact checks ────────────────────────────────────────────────


def test_json_schema_is_valid(json_schema: dict) -> None:
    """JSON Schema 2020-12 artifact must itself be valid (acceptance criterion)."""
    validator_cls = jsonschema.validators.validator_for(json_schema)
    validator_cls.check_schema(json_schema)


def test_json_schema_version() -> None:
    raw = json.loads(SCHEMA_PATH.read_text())
    assert raw["$schema"] == "https://json-schema.org/draft/2020-12/schema"


# ── ParentCapsule model tests ─────────────────────────────────────────────


def test_parent_capsule_default_status() -> None:
    p = ParentCapsule(
        global_run_id=new_ulid(),
        run_id=new_ulid(),
    )
    assert p.status == ParentStatus.RUNNING
    assert p.capsule_role == CapsuleRole.PARENT
    assert p.schema_version == "0.2.0"


def test_parent_capsule_partially_complete(json_schema: dict) -> None:
    """FR-01: schema accepts PARTIALLY_COMPLETE (cap-001)."""
    rid = new_ulid()
    p = ParentCapsule(
        global_run_id=new_ulid(),
        run_id=rid,
        status=ParentStatus.PARTIALLY_COMPLETE,
        children_expected=32,
        children_arrived=31,
    )
    assert p.status == ParentStatus.PARTIALLY_COMPLETE

    data = json.loads(p.model_dump_json())
    validator_cls = jsonschema.validators.validator_for(json_schema)
    validator = validator_cls(json_schema)
    errors = list(validator.iter_errors(data))
    assert not errors, f"Schema validation errors: {errors}"


def test_parent_capsule_children_fields() -> None:
    """FR-02: children_expected and children_arrived fields present."""
    p = ParentCapsule(
        global_run_id=new_ulid(),
        run_id=new_ulid(),
        children_expected=32,
        children_arrived=5,
    )
    assert p.children_expected == 32
    assert p.children_arrived == 5


def test_parent_capsule_all_statuses(json_schema: dict) -> None:
    """All ParentStatus values must be valid in schema."""
    for status in ParentStatus:
        p = ParentCapsule(
            global_run_id=new_ulid(),
            run_id=new_ulid(),
            status=status,
        )
        data = json.loads(p.model_dump_json())
        validator_cls = jsonschema.validators.validator_for(json_schema)
        validator = validator_cls(json_schema)
        errors = list(validator.iter_errors(data))
        assert not errors, f"Status {status} failed: {errors}"


def test_parent_capsule_rejects_invalid_status() -> None:
    """Schema must reject unknown status values."""
    with pytest.raises(Exception):
        ParentCapsule(
            global_run_id=new_ulid(),
            run_id=new_ulid(),
            status="INVALID_STATUS",  # type: ignore[arg-type]
        )


def test_parent_capsule_world_size_fields() -> None:
    """FR-21: world_size and expected_children fields."""
    p = ParentCapsule(
        global_run_id=new_ulid(),
        run_id=new_ulid(),
        world_size=8,
        expected_children=8,
    )
    assert p.world_size == 8
    assert p.children_expected == 8  # synced via model_validator


def test_parent_capsule_fail_mode_default() -> None:
    """FR-05: default fail_mode is fail-open."""
    p = ParentCapsule(global_run_id=new_ulid(), run_id=new_ulid())
    assert p.fail_mode == FailMode.fail_open


def test_parent_capsule_fail_mode_closed() -> None:
    p = ParentCapsule(
        global_run_id=new_ulid(),
        run_id=new_ulid(),
        fail_mode=FailMode.fail_closed,
    )
    assert p.fail_mode == FailMode.fail_closed


def test_parent_capsule_pending_timeout_default() -> None:
    p = ParentCapsule(global_run_id=new_ulid(), run_id=new_ulid())
    assert p.pending_parent_timeout_s == 86400.0


# ── ChildCapsule model tests ──────────────────────────────────────────────


def test_child_capsule_basic() -> None:
    parent_id = new_ulid()
    c = ChildCapsule(
        global_run_id=new_ulid(),
        run_id=new_ulid(),
        parent_run_id=parent_id,
        rank=0,
        world_size=4,
        distribution_role=DistributionRole.WORKER,
    )
    assert c.capsule_role == CapsuleRole.CHILD
    assert c.parent_run_id == parent_id
    assert c.rank == 0
    assert c.world_size == 4


def test_child_capsule_requires_parent_run_id() -> None:
    with pytest.raises(Exception):
        ChildCapsule(
            global_run_id=new_ulid(),
            run_id=new_ulid(),
            parent_run_id="not-a-valid-ulid",
        )


def test_child_capsule_validates_in_json_schema(json_schema: dict) -> None:
    c = ChildCapsule(
        global_run_id=new_ulid(),
        run_id=new_ulid(),
        parent_run_id=new_ulid(),
        rank=1,
        world_size=4,
    )
    data = json.loads(c.model_dump_json())
    validator_cls = jsonschema.validators.validator_for(json_schema)
    errors = list(validator_cls(json_schema).iter_errors(data))
    assert not errors, f"Schema errors: {errors}"


# ── OrphanPlaceholder tests ───────────────────────────────────────────────


def test_orphan_placeholder_is_synthetic() -> None:
    ph = OrphanPlaceholder(global_run_id=new_ulid(), run_id=new_ulid())
    assert ph.is_synthetic is True
    assert ph.capsule_role == CapsuleRole.PARENT


# ── LineageEdgeV2 tests ───────────────────────────────────────────────────


def test_lineage_edge_requires_edge_type() -> None:
    """FR-07: edge_type is required."""
    with pytest.raises(Exception):
        LineageEdgeV2(  # type: ignore[call-arg]
            edge_id=new_ulid(),
            source_run_id=new_ulid(),
            target_run_id=new_ulid(),
        )


def test_lineage_edge_all_types() -> None:
    """All four edge types must be valid (FR-07)."""
    for et in EdgeType:
        e = LineageEdgeV2(
            edge_id=new_ulid(),
            edge_type=et,
            source_run_id=new_ulid(),
            target_run_id=new_ulid(),
        )
        assert e.edge_type == et


def test_lineage_edge_attributes_optional() -> None:
    """FR-10: attributes map is optional."""
    e = LineageEdgeV2(
        edge_id=new_ulid(),
        edge_type=EdgeType.delegated_to,
        source_run_id=new_ulid(),
        target_run_id=new_ulid(),
        attributes={"delegation_reason": "tool_use"},
    )
    assert e.attributes == {"delegation_reason": "tool_use"}


def test_lineage_edge_unknown_attribute_preserved() -> None:
    """FR-10: unknown attribute key is preserved on read."""
    e = LineageEdgeV2(
        edge_id=new_ulid(),
        edge_type=EdgeType.spawned,
        source_run_id=new_ulid(),
        target_run_id=new_ulid(),
        attributes={"unknown_custom_key": "some_value", "another": 42},
    )
    assert e.attributes is not None
    assert e.attributes["unknown_custom_key"] == "some_value"


# ── ULID validation tests ─────────────────────────────────────────────────


def test_parent_capsule_rejects_invalid_ulid() -> None:
    """FR-25: schema validator rejects non-ULID run_id."""
    with pytest.raises(Exception):
        ParentCapsule(global_run_id="not-a-ulid", run_id=new_ulid())


def test_parent_capsule_accepts_uuid7() -> None:
    """FR-26: UUID v7 accepted as global_run_id."""
    from novafabric.capsule.ulid_util import new_uuid7
    uuid7 = new_uuid7()
    p = ParentCapsule(global_run_id=uuid7, run_id=new_ulid())
    assert p.global_run_id == uuid7
