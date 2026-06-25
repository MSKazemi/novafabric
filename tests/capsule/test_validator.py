# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for Validator state-machine (cap-001, FR-01–FR-06)."""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import make_parent_manifest, write_capsule

from novafabric.capsule.schema import FailMode, ParentStatus
from novafabric.capsule.ulid_util import new_ulid
from novafabric.capsule.validator import (
    DistributedCapsuleValidator,
    validate_capsule_json,
)
from novafabric.capsule.writer import ParentCapsuleTracker

# ── Schema validation (FR-01) ─────────────────────────────────────────────


def test_schema_accepts_partially_complete() -> None:
    """FR-01: schema validator accepts PARTIALLY_COMPLETE."""
    rid = new_ulid()
    manifest = make_parent_manifest(rid, new_ulid(), children_expected=32, children_arrived=31)
    manifest["status"] = "PARTIALLY_COMPLETE"
    validate_capsule_json(manifest)  # must not raise


def test_schema_accepts_complete() -> None:
    rid = new_ulid()
    manifest = make_parent_manifest(rid, new_ulid(), children_expected=4, children_arrived=4)
    manifest["status"] = "COMPLETE"
    validate_capsule_json(manifest)  # must not raise


def test_schema_accepts_failed() -> None:
    rid = new_ulid()
    manifest = make_parent_manifest(rid, new_ulid(), children_expected=4, children_arrived=0)
    manifest["status"] = "FAILED"
    validate_capsule_json(manifest)


def test_schema_rejects_unknown_enum_status() -> None:
    """FR-01: schema rejects unknown status values."""
    import jsonschema
    rid = new_ulid()
    manifest = make_parent_manifest(rid, new_ulid())
    manifest["status"] = "UNKNOWN_STATE"
    with pytest.raises(jsonschema.ValidationError):
        validate_capsule_json(manifest)


# ── State machine transitions (FR-03, FR-04) ─────────────────────────────


def test_transition_all_children_arrived_complete(tmp_path: Path) -> None:
    """FR-04: all children → COMPLETE."""
    spool = tmp_path / "spool"
    rid = new_ulid()
    manifest = make_parent_manifest(rid, new_ulid(), children_expected=4, children_arrived=4)
    write_capsule(spool, rid, manifest)

    tracker = ParentCapsuleTracker(spool / rid)
    new_status, updated = tracker.finalize(fail_mode=FailMode.fail_open)
    assert new_status == ParentStatus.COMPLETE.value


def test_transition_zero_children_failed(tmp_path: Path) -> None:
    """FR-04: 0 children → FAILED."""
    spool = tmp_path / "spool"
    rid = new_ulid()
    manifest = make_parent_manifest(rid, new_ulid(), children_expected=4, children_arrived=0)
    write_capsule(spool, rid, manifest)

    tracker = ParentCapsuleTracker(spool / rid)
    new_status, updated = tracker.finalize(fail_mode=FailMode.fail_open)
    assert new_status == ParentStatus.FAILED.value


def test_transition_partial_children_partially_complete(tmp_path: Path) -> None:
    """FR-03: 0 < arrived < expected → PARTIALLY_COMPLETE (fail-open)."""
    spool = tmp_path / "spool"
    rid = new_ulid()
    manifest = make_parent_manifest(rid, new_ulid(), children_expected=32, children_arrived=31)
    write_capsule(spool, rid, manifest)

    tracker = ParentCapsuleTracker(spool / rid)
    new_status, updated = tracker.finalize(fail_mode=FailMode.fail_open)
    assert new_status == ParentStatus.PARTIALLY_COMPLETE.value
    assert updated["children_arrived"] == 31


def test_transition_partial_children_fail_closed(tmp_path: Path) -> None:
    """FR-05: fail_mode=fail-closed + partial → FAILED."""
    spool = tmp_path / "spool"
    rid = new_ulid()
    manifest = make_parent_manifest(rid, new_ulid(), children_expected=4, children_arrived=3)
    write_capsule(spool, rid, manifest)

    tracker = ParentCapsuleTracker(spool / rid)
    new_status, updated = tracker.finalize(fail_mode=FailMode.fail_closed)
    assert new_status == ParentStatus.FAILED.value


def test_transition_already_terminal_no_op(tmp_path: Path) -> None:
    """Finalize on already-terminal capsule is a no-op."""
    spool = tmp_path / "spool"
    rid = new_ulid()
    manifest = make_parent_manifest(rid, new_ulid(), children_expected=4, children_arrived=4)
    manifest["status"] = "COMPLETE"
    write_capsule(spool, rid, manifest)

    tracker = ParentCapsuleTracker(spool / rid)
    new_status, updated = tracker.finalize()
    assert new_status == "COMPLETE"  # unchanged


# ── register_child_arrival (FR-02) ────────────────────────────────────────


def test_register_child_increments_arrived(tmp_path: Path) -> None:
    """FR-02: committing N children increments children_arrived."""
    spool = tmp_path / "spool"
    rid = new_ulid()
    manifest = make_parent_manifest(rid, new_ulid(), children_expected=4, children_arrived=0)
    write_capsule(spool, rid, manifest)

    tracker = ParentCapsuleTracker(spool / rid)
    for i in range(1, 5):
        updated = tracker.register_child_arrival(f"child_{i}")
        assert updated["children_arrived"] == i


# ── FR-03 integration test (32 expected, 31 arrived) ────────────────────


def test_fr03_integration_32_expected_31_arrived(tmp_path: Path) -> None:
    """FR-03: with 32 expected, 31 arrived, timeout elapsed → PARTIALLY_COMPLETE."""
    spool = tmp_path / "spool"
    parent_id = new_ulid()
    global_id = new_ulid()
    manifest = make_parent_manifest(
        parent_id,
        global_id,
        children_expected=32,
        children_arrived=0,
        pending_parent_timeout_s=0.01,  # 10ms for testing
    )
    write_capsule(spool, parent_id, manifest)

    tracker = ParentCapsuleTracker(spool / parent_id)
    # Simulate 31 children arriving
    for i in range(31):
        tracker.register_child_arrival(f"child_{i}")

    # Finalize (timeout elapsed conceptually)
    new_status, updated = tracker.finalize(fail_mode=FailMode.fail_open)
    assert new_status == ParentStatus.PARTIALLY_COMPLETE.value
    assert updated["children_arrived"] == 31


# ── DistributedCapsuleValidator exit codes (FR-06) ────────────────────────


def test_validate_distributed_complete_exit_0(tmp_path: Path) -> None:
    """FR-06: COMPLETE → exit code 0."""
    spool = tmp_path / "spool"
    rid = new_ulid()
    manifest = make_parent_manifest(
        rid, new_ulid(), children_expected=4, children_arrived=4, status="COMPLETE"
    )
    write_capsule(spool, rid, manifest)

    validator = DistributedCapsuleValidator(spool / rid)
    result = validator.validate_distributed(rid)
    assert result.exit_code == 0
    assert result.status == "COMPLETE"


def test_validate_distributed_failed_exit_1(tmp_path: Path) -> None:
    """FR-06: FAILED → exit code 1."""
    spool = tmp_path / "spool"
    rid = new_ulid()
    manifest = make_parent_manifest(
        rid, new_ulid(), children_expected=4, children_arrived=0, status="FAILED"
    )
    write_capsule(spool, rid, manifest)

    validator = DistributedCapsuleValidator(spool / rid)
    result = validator.validate_distributed(rid)
    assert result.exit_code == 1
    assert result.status == "FAILED"


def test_validate_distributed_partially_complete_exit_2(tmp_path: Path) -> None:
    """FR-06: PARTIALLY_COMPLETE → exit code 2."""
    spool = tmp_path / "spool"
    rid = new_ulid()
    manifest = make_parent_manifest(
        rid, new_ulid(), children_expected=4, children_arrived=3, status="PARTIALLY_COMPLETE"
    )
    write_capsule(spool, rid, manifest)

    validator = DistributedCapsuleValidator(spool / rid)
    result = validator.validate_distributed(rid)
    assert result.exit_code == 2
    assert result.status == "PARTIALLY_COMPLETE"


def test_validate_distributed_three_exit_codes(tmp_path: Path) -> None:
    """FR-06: Three test parents return three expected exit codes."""
    spool = tmp_path / "spool"
    cases = [
        ("COMPLETE", 4, 4, 0),
        ("FAILED", 4, 0, 1),
        ("PARTIALLY_COMPLETE", 4, 2, 2),
    ]
    for status, expected, arrived, expected_exit in cases:
        rid = new_ulid()
        manifest = make_parent_manifest(
            rid, new_ulid(), children_expected=expected,
            children_arrived=arrived, status=status,
        )
        write_capsule(spool, rid, manifest)
        validator = DistributedCapsuleValidator(spool / rid)
        result = validator.validate_distributed(rid)
        assert result.exit_code == expected_exit, (
            f"Status {status}: expected exit {expected_exit}, got {result.exit_code}"
        )
