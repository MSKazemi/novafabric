# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for CapsuleWriter — Phase 1 in-process writer (cap-004, FR-17–FR-20)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novafabric.capsule.env_contract import CapsuleEnvConfig
from novafabric.capsule.schema import CapsuleRole, DistributionRole, EdgeType, FailMode
from novafabric.capsule.ulid_util import new_ulid
from novafabric.capsule.writer import CapsuleWriter


def _make_config(
    spool: Path,
    *,
    capsule_role: CapsuleRole = CapsuleRole.STANDALONE,
    parent_run_id: str | None = None,
    world_size: int | None = None,
    rank: int | None = None,
) -> CapsuleEnvConfig:
    return CapsuleEnvConfig(
        global_run_id=new_ulid(),
        parent_run_id=parent_run_id,
        capsule_dir=str(spool),
        rank=rank,
        world_size=world_size,
        distribution_role=DistributionRole.WORKER if parent_run_id else None,
        capsule_role=capsule_role,
        fail_mode=FailMode.fail_open,
        pending_parent_timeout_s=86400.0,
        warnings=[],
    )


def test_writer_commit_creates_capsule_json(tmp_path: Path) -> None:
    """FR-17: commit writes capsule.json to local spool."""
    spool = tmp_path / "spool"
    config = _make_config(spool)
    writer = CapsuleWriter(config)
    writer.start()
    capsule_dir = writer.commit(status="COMPLETE")

    assert capsule_dir.exists()
    assert (capsule_dir / "capsule.json").exists()
    assert (capsule_dir / "lineage.jsonl").exists()


def test_writer_commit_manifest_fields(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    config = _make_config(spool)
    writer = CapsuleWriter(config)
    writer.start()
    capsule_dir = writer.commit(status="COMPLETE")

    manifest = json.loads((capsule_dir / "capsule.json").read_text())
    assert manifest["schema_version"] == "0.2.0"
    assert manifest["status"] == "COMPLETE"
    assert manifest["global_run_id"] == config.global_run_id
    assert manifest["run_id"] == writer.run_id


def test_writer_commit_with_parent_run_id(tmp_path: Path) -> None:
    """FR-18: child writer reads parent_run_id from config."""
    spool = tmp_path / "spool"
    parent_id = new_ulid()
    config = _make_config(spool, capsule_role=CapsuleRole.CHILD, parent_run_id=parent_id)
    writer = CapsuleWriter(config)
    writer.start()
    capsule_dir = writer.commit(status="COMPLETE")

    manifest = json.loads((capsule_dir / "capsule.json").read_text())
    assert manifest["parent_run_id"] == parent_id
    assert manifest["capsule_role"] == "CHILD"


def test_writer_commit_is_atomic(tmp_path: Path) -> None:
    """FR-17: os.rename() atomic commit — no partial directory visible."""
    spool = tmp_path / "spool"
    config = _make_config(spool)
    writer = CapsuleWriter(config)
    writer.start()

    # Verify no .tmp_ dirs in spool after commit
    capsule_dir = writer.commit()
    tmp_dirs = list(spool.glob(".tmp_*"))
    assert not tmp_dirs, f"Leftover temp dirs: {tmp_dirs}"
    assert capsule_dir.exists()


def test_writer_commit_raises_if_not_started(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    config = _make_config(spool)
    writer = CapsuleWriter(config)
    with pytest.raises(RuntimeError, match="start()"):
        writer.commit()


def test_writer_commit_raises_if_called_twice(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    config = _make_config(spool)
    writer = CapsuleWriter(config)
    writer.start()
    writer.commit()
    with pytest.raises(RuntimeError, match="already called"):
        writer.commit()


def test_writer_add_lineage_edge(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    config = _make_config(spool)
    writer = CapsuleWriter(config)
    writer.start()
    target_run_id = new_ulid()
    edge = writer.add_lineage_edge(target_run_id, EdgeType.contains)
    capsule_dir = writer.commit()

    lineage_text = (capsule_dir / "lineage.jsonl").read_text()
    records = [json.loads(line) for line in lineage_text.splitlines() if line.strip()]
    assert len(records) == 1
    assert records[0]["edge_type"] == "contains"
    assert records[0]["target_run_id"] == target_run_id
    assert records[0]["edge_id"] == edge.edge_id


def test_writer_from_env_standalone_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-20: without env vars, defaults to STANDALONE + synthesises global_run_id."""
    monkeypatch.delenv("NOVAFABRIC_GLOBAL_RUN_ID", raising=False)
    monkeypatch.delenv("NOVAFABRIC_PARENT_RUN_ID", raising=False)
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(tmp_path / "spool"))
    monkeypatch.delenv("NOVAFABRIC_WORLD_SIZE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_DISTRIBUTION_ROLE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_FAIL_MODE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_PENDING_PARENT_TIMEOUT", raising=False)
    monkeypatch.delenv("WORLD_SIZE", raising=False)
    monkeypatch.delenv("SLURM_NTASKS", raising=False)
    monkeypatch.delenv("SLURM_ARRAY_TASK_COUNT", raising=False)
    monkeypatch.delenv("OMPI_COMM_WORLD_SIZE", raising=False)
    monkeypatch.delenv("RAY_WORLD_SIZE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_K8S_JOB_COMPLETIONS", raising=False)
    monkeypatch.delenv("NOVAFABRIC_RANK", raising=False)

    writer = CapsuleWriter.from_env()
    assert writer.capsule_role == CapsuleRole.STANDALONE


def test_writer_from_env_child_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-18: NOVAFABRIC_PARENT_RUN_ID sets CHILD role."""
    parent_id = new_ulid()
    monkeypatch.setenv("NOVAFABRIC_GLOBAL_RUN_ID", new_ulid())
    monkeypatch.setenv("NOVAFABRIC_PARENT_RUN_ID", parent_id)
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(tmp_path / "spool"))
    monkeypatch.setenv("NOVAFABRIC_RANK", "2")
    monkeypatch.setenv("NOVAFABRIC_WORLD_SIZE", "4")
    monkeypatch.setenv("NOVAFABRIC_DISTRIBUTION_ROLE", "WORKER")
    monkeypatch.delenv("NOVAFABRIC_FAIL_MODE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_PENDING_PARENT_TIMEOUT", raising=False)
    monkeypatch.delenv("WORLD_SIZE", raising=False)
    monkeypatch.delenv("SLURM_NTASKS", raising=False)
    monkeypatch.delenv("SLURM_ARRAY_TASK_COUNT", raising=False)
    monkeypatch.delenv("OMPI_COMM_WORLD_SIZE", raising=False)
    monkeypatch.delenv("RAY_WORLD_SIZE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_K8S_JOB_COMPLETIONS", raising=False)

    writer = CapsuleWriter.from_env()
    assert writer.capsule_role == CapsuleRole.CHILD
    writer.start()
    capsule_dir = writer.commit()
    manifest = json.loads((capsule_dir / "capsule.json").read_text())
    assert manifest["parent_run_id"] == parent_id
    assert manifest["rank"] == 2


def test_writer_set_extra(tmp_path: Path) -> None:
    """Extra fields are written to the manifest."""
    spool = tmp_path / "spool"
    config = _make_config(spool)
    writer = CapsuleWriter(config)
    writer.start()
    writer.set_extra(custom_key="custom_value", count=42)
    capsule_dir = writer.commit()

    manifest = json.loads((capsule_dir / "capsule.json").read_text())
    assert manifest["custom_key"] == "custom_value"
    assert manifest["count"] == 42
