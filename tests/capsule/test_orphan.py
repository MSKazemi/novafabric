# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for OrphanManager — ORPHAN_PARENT placeholder logic (cap-003, FR-14, FR-15)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from helpers import make_child_manifest, make_parent_manifest, write_capsule

from novafabric.capsule.orphan import OrphanManager, orphan_placeholder_run_id
from novafabric.capsule.ulid_util import is_valid_ulid, new_ulid


def test_orphan_placeholder_run_id_is_deterministic() -> None:
    """FR-14: deterministic run_id from parent_run_id."""
    parent_id = new_ulid()
    ph_id1 = orphan_placeholder_run_id(parent_id)
    ph_id2 = orphan_placeholder_run_id(parent_id)
    assert ph_id1 == ph_id2


def test_orphan_placeholder_run_id_is_valid_ulid() -> None:
    parent_id = new_ulid()
    ph_id = orphan_placeholder_run_id(parent_id)
    assert is_valid_ulid(ph_id), f"Not a valid ULID: {ph_id!r}"
    assert len(ph_id) == 26


def test_orphan_placeholder_run_id_differs_from_parent() -> None:
    parent_id = new_ulid()
    ph_id = orphan_placeholder_run_id(parent_id)
    assert ph_id != parent_id


def test_orphan_placeholder_created_on_timeout(tmp_path: Path) -> None:
    """FR-14: after timeout, child gets linked to synthetic ORPHAN_PARENT."""
    spool = tmp_path / "spool"
    parent_id = new_ulid()
    child_id = new_ulid()
    global_id = parent_id  # use parent_id as global for simplicity

    # Write child only (no parent)
    # Create child with timestamp in the past
    child_manifest = make_child_manifest(child_id, global_id, parent_id)
    child_manifest["created_at"] = "2020-01-01T00:00:00+00:00"  # far in the past
    write_capsule(spool, child_id, child_manifest)

    # Use a short timeout
    manager = OrphanManager(spool, pending_parent_timeout_s=0.001, clock_fn=time.time)
    created = manager.check_orphan_timeouts()

    assert parent_id in created

    # Verify placeholder exists
    ph_id = orphan_placeholder_run_id(parent_id)
    ph_path = spool / ph_id / "capsule.json"
    assert ph_path.exists()
    ph = json.loads(ph_path.read_text())
    assert ph["is_synthetic"] is True
    assert ph["run_id"] == ph_id


def test_orphan_placeholder_idempotent_creation(tmp_path: Path) -> None:
    """FR-14: ensure_placeholder is idempotent."""
    spool = tmp_path / "spool"
    parent_id = new_ulid()

    manager = OrphanManager(spool, pending_parent_timeout_s=1.0)
    ph1 = manager.ensure_placeholder(parent_id)
    ph2 = manager.ensure_placeholder(parent_id)
    assert ph1["run_id"] == ph2["run_id"]


def test_real_parent_replaces_orphan_placeholder(tmp_path: Path) -> None:
    """FR-15: real parent arrival replaces orphan placeholder."""
    spool = tmp_path / "spool"
    parent_id = new_ulid()
    global_id = new_ulid()

    manager = OrphanManager(spool, pending_parent_timeout_s=0.001)

    # Create orphan placeholder first
    manager.ensure_placeholder(parent_id)
    ph_id = orphan_placeholder_run_id(parent_id)
    assert (spool / ph_id / "capsule.json").exists()

    # Real parent arrives
    real_manifest = make_parent_manifest(
        parent_id, global_id, children_expected=4, children_arrived=0
    )
    manager.replace_with_real_parent(real_manifest)

    # Real parent exists
    real_path = spool / parent_id / "capsule.json"
    assert real_path.exists()
    real = json.loads(real_path.read_text())
    assert real.get("is_synthetic") is None or real.get("is_synthetic") is False

    # Placeholder is superseded
    ph_data = json.loads((spool / ph_id / "capsule.json").read_text())
    assert ph_data.get("superseded_by") == parent_id


def test_child_run_id_unchanged_after_real_parent_arrival(tmp_path: Path) -> None:
    """FR-15: child rows unchanged when real parent arrives."""
    spool = tmp_path / "spool"
    parent_id = new_ulid()
    global_id = new_ulid()
    child_id = new_ulid()

    # Write orphan child
    child_manifest = make_child_manifest(child_id, global_id, parent_id, rank=0)
    write_capsule(spool, child_id, child_manifest)

    manager = OrphanManager(spool)
    # Real parent arrives
    real_manifest = make_parent_manifest(parent_id, global_id, children_expected=1)
    manager.replace_with_real_parent(real_manifest)

    # Child still has same parent_run_id
    child_data = json.loads((spool / child_id / "capsule.json").read_text())
    assert child_data["parent_run_id"] == parent_id
    assert child_data["run_id"] == child_id


def test_no_placeholder_if_real_parent_exists(tmp_path: Path) -> None:
    """ensure_placeholder returns real parent if it already exists."""
    spool = tmp_path / "spool"
    parent_id = new_ulid()
    global_id = new_ulid()

    real_manifest = make_parent_manifest(parent_id, global_id, children_expected=2)
    write_capsule(spool, parent_id, real_manifest)

    manager = OrphanManager(spool)
    result = manager.ensure_placeholder(parent_id)
    assert result["run_id"] == parent_id
    assert not result.get("is_synthetic")
