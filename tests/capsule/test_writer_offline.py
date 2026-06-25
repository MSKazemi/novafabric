# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Network-isolation test for CapsuleWriter Phase 1 (cap-004, FR-17).

Monkeypatches socket.socket to raise OSError so any network call
would be caught. The writer must complete successfully regardless.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from novafabric.capsule.env_contract import CapsuleEnvConfig
from novafabric.capsule.schema import CapsuleRole, FailMode
from novafabric.capsule.ulid_util import new_ulid
from novafabric.capsule.writer import CapsuleWriter


def _make_offline_config(spool: Path) -> CapsuleEnvConfig:
    return CapsuleEnvConfig(
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


class _NoNetworkSocket:
    """Fake socket that raises OSError on any network operation."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise OSError("Network access denied in Phase 1 capsule creation")


def test_writer_creates_capsule_in_network_isolated_context(tmp_path: Path) -> None:
    """FR-17: capsule creation must succeed with no network access.

    We monkeypatch socket.socket to raise OSError. The writer must still:
    - write capsule.json to local disk
    - write lineage.jsonl to local disk
    - complete without raising
    """
    spool = tmp_path / "spool"
    config = _make_offline_config(spool)
    writer = CapsuleWriter(config)
    writer.start()

    with patch("socket.socket", _NoNetworkSocket):
        capsule_dir = writer.commit(status="COMPLETE")

    assert (capsule_dir / "capsule.json").exists()
    assert (capsule_dir / "lineage.jsonl").exists()

    manifest = json.loads((capsule_dir / "capsule.json").read_text())
    assert manifest["status"] == "COMPLETE"
    assert manifest["schema_version"] == "0.2.0"


def test_writer_lineage_written_in_network_isolated_context(tmp_path: Path) -> None:
    """Lineage edges are written without network access."""
    spool = tmp_path / "spool"
    config = _make_offline_config(spool)
    writer = CapsuleWriter(config)
    writer.start()
    target = new_ulid()
    from novafabric.capsule.schema import EdgeType
    writer.add_lineage_edge(target, EdgeType.spawned)

    with patch("socket.socket", _NoNetworkSocket):
        capsule_dir = writer.commit()

    lines = [
        ln for ln in (capsule_dir / "lineage.jsonl").read_text().splitlines()
        if ln.strip()
    ]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["edge_type"] == "spawned"
    assert record["target_run_id"] == target


def test_signing_inputs_not_required_in_phase1(tmp_path: Path) -> None:
    """Phase 1 writer does NOT write signing inputs — NovaSeal is Phase 2."""
    spool = tmp_path / "spool"
    config = _make_offline_config(spool)
    writer = CapsuleWriter(config)
    writer.start()

    with patch("socket.socket", _NoNetworkSocket):
        capsule_dir = writer.commit()

    # No .sig, .dsse, or seal files expected in Phase 1
    sig_files = list(capsule_dir.glob("*.sig")) + list(capsule_dir.glob("*.dsse"))
    assert not sig_files, f"Unexpected signing files in Phase 1: {sig_files}"
