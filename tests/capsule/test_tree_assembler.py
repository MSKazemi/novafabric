# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for CapsuleTreeAssembler — lazy read path (cap-003, FR-12, FR-13)."""

from __future__ import annotations

from pathlib import Path

from helpers import make_child_manifest, make_parent_manifest, write_capsule

from novafabric.capsule.tree_assembler import CapsuleTreeAssembler, CyclicLineageError
from novafabric.capsule.ulid_util import new_ulid


def test_orphan_children_committed_without_parent(tmp_path: Path) -> None:
    """FR-12: 31 child capsules committed without parent; all reads succeed."""
    spool = tmp_path / "spool"
    parent_id = new_ulid()
    global_id = new_ulid()
    child_ids = [new_ulid() for _ in range(31)]

    for i, cid in enumerate(child_ids):
        manifest = make_child_manifest(cid, global_id, parent_id, rank=i, world_size=31)
        write_capsule(spool, cid, manifest)

    assembler = CapsuleTreeAssembler(spool)
    children = assembler.get_children(parent_id)
    assert len(children) == 31
    for c in children:
        assert c["parent_run_id"] == parent_id


def test_orphan_children_preserve_parent_run_id(tmp_path: Path) -> None:
    """FR-12: parent_run_id is preserved on orphan children."""
    spool = tmp_path / "spool"
    parent_id = new_ulid()
    global_id = new_ulid()
    child_id = new_ulid()

    manifest = make_child_manifest(child_id, global_id, parent_id)
    write_capsule(spool, child_id, manifest)

    assembler = CapsuleTreeAssembler(spool)
    children = assembler.get_children(parent_id)
    assert len(children) == 1
    assert children[0]["parent_run_id"] == parent_id


def test_show_with_children_orphan_parent_unknown(tmp_path: Path) -> None:
    """FR-13: nova show <orphan_parent_id> --with-children shows children with parent: unknown."""
    spool = tmp_path / "spool"
    parent_id = new_ulid()
    global_id = new_ulid()
    child_id = new_ulid()

    # Only write child — parent not in spool
    manifest = make_child_manifest(child_id, global_id, parent_id)
    write_capsule(spool, child_id, manifest)

    assembler = CapsuleTreeAssembler(spool)
    tree = assembler.assemble_tree(parent_id, include_orphans=True)
    assert tree.root.is_synthetic  # synthetic placeholder created
    # Orphan children should be in orphan_children
    orphan_children = assembler.list_orphan_children(parent_id)
    assert len(orphan_children) == 1
    assert orphan_children[0].get("_orphan_parent") is True


def test_tree_assembly_with_real_parent(tmp_path: Path) -> None:
    """FR-13: assembler builds correct tree when real parent and children exist."""
    spool = tmp_path / "spool"
    parent_id = new_ulid()
    global_id = new_ulid()
    child_ids = [new_ulid() for _ in range(3)]

    # Write parent
    write_capsule(spool, parent_id, make_parent_manifest(
        parent_id, global_id, children_expected=3, children_arrived=3, status="COMPLETE"
    ))
    # Write children
    for i, cid in enumerate(child_ids):
        write_capsule(spool, cid, make_child_manifest(cid, global_id, parent_id, rank=i))

    assembler = CapsuleTreeAssembler(spool)
    tree = assembler.assemble_tree(parent_id)
    assert not tree.root.is_synthetic
    assert tree.root.run_id == parent_id
    assert len(tree.root.children) == 3
    assert tree.total_nodes == 4


def test_empty_spool_returns_synthetic_root(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    assembler = CapsuleTreeAssembler(spool)
    tree = assembler.assemble_tree(new_ulid())
    assert tree.root.is_synthetic
    assert tree.total_nodes == 1


def test_scan_ignores_non_capsule_dirs(tmp_path: Path) -> None:
    """Directories without capsule.json are ignored."""
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / "not_a_capsule").mkdir()
    (spool / "not_a_capsule" / "README.txt").write_text("hello")

    assembler = CapsuleTreeAssembler(spool)
    manifests = assembler._scan_capsules()
    assert not manifests


def test_cyclic_parent_reference_raises_cyclic_lineage_error(tmp_path: Path) -> None:
    """P0 security: A→B→A circular reference must raise CyclicLineageError, not recurse infinitely."""
    import json

    spool = tmp_path / "spool"
    id_a = new_ulid()
    id_b = new_ulid()

    # Capsule A claims B as its parent; capsule B claims A as its parent.
    manifest_a = {
        "schema_version": "0.2.0",
        "run_id": id_a,
        "global_run_id": id_a,
        "parent_run_id": id_b,
        "capsule_role": "CHILD",
        "status": "COMPLETE",
    }
    manifest_b = {
        "schema_version": "0.2.0",
        "run_id": id_b,
        "global_run_id": id_b,
        "parent_run_id": id_a,
        "capsule_role": "CHILD",
        "status": "COMPLETE",
    }
    for mid, mdata in [(id_a, manifest_a), (id_b, manifest_b)]:
        run_dir = spool / mid
        run_dir.mkdir(parents=True)
        (run_dir / "capsule.json").write_text(json.dumps(mdata))

    assembler = CapsuleTreeAssembler(spool)
    import pytest
    with pytest.raises(CyclicLineageError, match=r"[Cc]ycl"):
        assembler.assemble_tree(id_a)


def test_deep_acyclic_tree_raises_depth_exceeded(tmp_path: Path) -> None:
    """A deep but acyclic chain must raise TreeDepthExceededError, not RecursionError."""
    import json

    from novafabric.capsule.tree_assembler import MAX_TREE_DEPTH, TreeDepthExceededError

    spool = tmp_path / "spool"
    # Linear chain root -> c1 -> c2 -> ... longer than MAX_TREE_DEPTH.
    ids = [new_ulid() for _ in range(MAX_TREE_DEPTH + 5)]
    for i, rid in enumerate(ids):
        manifest = {
            "schema_version": "0.2.0",
            "run_id": rid,
            "global_run_id": ids[0],
            "parent_run_id": ids[i - 1] if i > 0 else None,
            "capsule_role": "PARENT" if i == 0 else "CHILD",
            "status": "COMPLETE",
        }
        run_dir = spool / rid
        run_dir.mkdir(parents=True)
        (run_dir / "capsule.json").write_text(json.dumps(manifest))

    assembler = CapsuleTreeAssembler(spool)
    import pytest
    with pytest.raises(TreeDepthExceededError, match=str(MAX_TREE_DEPTH)):
        assembler.assemble_tree(ids[0])
