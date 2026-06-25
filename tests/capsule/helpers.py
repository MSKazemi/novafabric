# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Shared helper functions for capsule Phase 3 tests.

This module is importable via sys.path manipulation in conftest.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def make_parent_manifest(
    run_id: str,
    global_run_id: str,
    *,
    children_expected: int | None = None,
    children_arrived: int = 0,
    status: str = "RUNNING",
    fail_mode: str = "fail-open",
    world_size: int | None = None,
    pending_parent_timeout_s: float = 86400.0,
) -> dict[str, Any]:
    from datetime import datetime, timezone
    return {
        "schema_version": "0.2.0",
        "capsule_role": "PARENT",
        "global_run_id": global_run_id,
        "run_id": run_id,
        "parent_run_id": None,
        "status": status,
        "fail_mode": fail_mode,
        "children_expected": children_expected,
        "expected_children": children_expected,
        "children_arrived": children_arrived,
        "world_size": world_size,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "pending_parent_timeout_s": pending_parent_timeout_s,
    }


def make_child_manifest(
    run_id: str,
    global_run_id: str,
    parent_run_id: str,
    *,
    rank: int | None = None,
    world_size: int | None = None,
    status: str = "COMPLETE",
) -> dict[str, Any]:
    from datetime import datetime, timezone
    return {
        "schema_version": "0.2.0",
        "capsule_role": "CHILD",
        "global_run_id": global_run_id,
        "run_id": run_id,
        "parent_run_id": parent_run_id,
        "status": status,
        "rank": rank,
        "world_size": world_size,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def write_capsule(spool: Path, run_id: str, manifest: dict[str, Any]) -> Path:
    """Write a capsule manifest to spool/run_id/capsule.json."""
    run_dir = spool / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    capsule_path = run_dir / "capsule.json"
    capsule_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return run_dir
