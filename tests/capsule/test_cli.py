# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""CLI tests for Phase 3 capsule commands (FR-06, FR-09, FR-13, FR-27)."""

from __future__ import annotations

import json
from pathlib import Path

from helpers import make_child_manifest, make_parent_manifest, write_capsule
from typer.testing import CliRunner

from novafabric.capsule.cli import capsule_phase3_app
from novafabric.capsule.ulid_util import is_valid_ulid, new_ulid

runner = CliRunner()


# ── nova new-run-id (FR-27) ───────────────────────────────────────────────


def test_new_run_id_prints_ulid() -> None:
    """FR-27: nova new-run-id prints a fresh ULID."""
    result = runner.invoke(capsule_phase3_app, ["new-run-id"])
    assert result.exit_code == 0
    ulid = result.output.strip()
    assert is_valid_ulid(ulid), f"Output is not a valid ULID: {ulid!r}"
    assert len(ulid) == 26


def test_new_run_id_is_unique() -> None:
    """Each invocation produces a different ULID."""
    results = [runner.invoke(capsule_phase3_app, ["new-run-id"]).output.strip()
               for _ in range(5)]
    assert len(set(results)) == 5


# ── nova validate-distributed (FR-06) ────────────────────────────────────


def test_validate_distributed_complete_exit_0(tmp_path: Path) -> None:
    """FR-06: COMPLETE → exit code 0."""
    spool = tmp_path / "spool"
    rid = new_ulid()
    manifest = make_parent_manifest(
        rid, new_ulid(), children_expected=4, children_arrived=4, status="COMPLETE"
    )
    cap_dir = write_capsule(spool, rid, manifest)

    result = runner.invoke(capsule_phase3_app, ["validate-distributed", str(cap_dir)])
    assert result.exit_code == 0


def test_validate_distributed_failed_exit_1(tmp_path: Path) -> None:
    """FR-06: FAILED → exit code 1."""
    spool = tmp_path / "spool"
    rid = new_ulid()
    manifest = make_parent_manifest(
        rid, new_ulid(), children_expected=4, children_arrived=0, status="FAILED"
    )
    cap_dir = write_capsule(spool, rid, manifest)

    result = runner.invoke(capsule_phase3_app, ["validate-distributed", str(cap_dir)])
    assert result.exit_code == 1


def test_validate_distributed_partially_complete_exit_2(tmp_path: Path) -> None:
    """FR-06: PARTIALLY_COMPLETE → exit code 2."""
    spool = tmp_path / "spool"
    rid = new_ulid()
    manifest = make_parent_manifest(
        rid, new_ulid(), children_expected=4, children_arrived=2, status="PARTIALLY_COMPLETE"
    )
    cap_dir = write_capsule(spool, rid, manifest)

    result = runner.invoke(capsule_phase3_app, ["validate-distributed", str(cap_dir)])
    assert result.exit_code == 2


def test_validate_distributed_not_a_dir(tmp_path: Path) -> None:
    result = runner.invoke(
        capsule_phase3_app, ["validate-distributed", str(tmp_path / "nonexistent")]
    )
    assert result.exit_code == 1


# ── nova show --with-children (FR-13) ────────────────────────────────────


def test_show_with_children_text_output(tmp_path: Path) -> None:
    """FR-13: show tree for parent with children."""
    spool = tmp_path / "spool"
    parent_id = new_ulid()
    global_id = new_ulid()
    child_ids = [new_ulid() for _ in range(2)]

    write_capsule(spool, parent_id, make_parent_manifest(
        parent_id, global_id, children_expected=2, children_arrived=2, status="COMPLETE"
    ))
    for i, cid in enumerate(child_ids):
        write_capsule(spool, cid, make_child_manifest(cid, global_id, parent_id, rank=i))

    result = runner.invoke(
        capsule_phase3_app,
        ["show", str(spool), "--run-id", parent_id, "--with-children"],
    )
    assert result.exit_code == 0
    assert "Total" in result.output


def test_show_with_children_json_output(tmp_path: Path) -> None:
    """FR-13: JSON output for show with children."""
    spool = tmp_path / "spool"
    parent_id = new_ulid()
    global_id = new_ulid()

    write_capsule(spool, parent_id, make_parent_manifest(
        parent_id, global_id, children_expected=1, children_arrived=1, status="COMPLETE"
    ))
    child_id = new_ulid()
    write_capsule(spool, child_id, make_child_manifest(child_id, global_id, parent_id))

    result = runner.invoke(
        capsule_phase3_app,
        ["show", str(spool), "--run-id", parent_id, "--with-children", "--output", "json"],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["run_id"] == parent_id
    assert len(data["children"]) == 1


def test_show_orphan_returns_unknown_parent(tmp_path: Path) -> None:
    """FR-13: orphan children appear with parent: unknown when viewed via spool."""
    spool = tmp_path / "spool"
    parent_id = new_ulid()
    global_id = new_ulid()
    child_id = new_ulid()

    # Only write child (orphan)
    write_capsule(spool, child_id, make_child_manifest(child_id, global_id, parent_id))

    result = runner.invoke(
        capsule_phase3_app,
        ["show", str(spool), "--run-id", parent_id, "--with-children"],
    )
    assert result.exit_code == 0
    assert "synthetic" in result.output or "unknown" in result.output or parent_id in result.output


# ── nova lineage --edge-types (FR-09) ────────────────────────────────────


def test_lineage_filtered_delegated_to(tmp_path: Path) -> None:
    """FR-09: --edge-types=delegated_to returns exactly delegation edges."""
    from novafabric.capsule.env_contract import CapsuleEnvConfig
    from novafabric.capsule.schema import CapsuleRole, EdgeType, FailMode
    from novafabric.capsule.writer import CapsuleWriter

    spool = tmp_path / "spool"
    run_ids: dict[str, str] = {}

    for et in EdgeType:
        config = CapsuleEnvConfig(
            global_run_id=new_ulid(),
            parent_run_id=None,
            capsule_dir=str(spool),
            rank=None,
            world_size=None,
            distribution_role=None,
            capsule_role=CapsuleRole.STANDALONE,
            fail_mode=FailMode.fail_open,
            pending_parent_timeout_s=86400.0,
            warnings=[],
        )
        w = CapsuleWriter(config)
        w.start()
        w.add_lineage_edge(new_ulid(), et)
        w.commit()
        run_ids[et.value] = w.run_id

    # Query for delegated_to only
    result = runner.invoke(
        capsule_phase3_app,
        ["lineage", run_ids["delegated_to"], "--spool-dir", str(spool),
         "--edge-types", "delegated_to", "--output", "json"],
    )
    assert result.exit_code == 0
    edges = json.loads(result.output)
    assert len(edges) == 1
    assert edges[0]["edge_type"] == "delegated_to"


def test_lineage_invalid_edge_type(tmp_path: Path) -> None:
    result = runner.invoke(
        capsule_phase3_app,
        ["lineage", new_ulid(), "--spool-dir", str(tmp_path),
         "--edge-types", "invalid_type"],
    )
    assert result.exit_code == 1
    assert "Unknown edge types" in result.output


def test_lineage_no_edges_returns_empty(tmp_path: Path) -> None:
    result = runner.invoke(
        capsule_phase3_app,
        ["lineage", new_ulid(), "--spool-dir", str(tmp_path), "--output", "json"],
    )
    assert result.exit_code == 0
    assert json.loads(result.output) == []
