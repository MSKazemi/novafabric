# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for world_size / expected_children env-var mapping (cap-006, FR-21–FR-24)."""

from __future__ import annotations

from pathlib import Path

import pytest

from novafabric.capsule.env_contract import read_env
from novafabric.capsule.ulid_util import new_ulid


def test_world_size_from_novafabric_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NOVAFABRIC_WORLD_SIZE takes precedence."""
    monkeypatch.setenv("NOVAFABRIC_GLOBAL_RUN_ID", new_ulid())
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(tmp_path))
    monkeypatch.setenv("NOVAFABRIC_WORLD_SIZE", "16")
    monkeypatch.delenv("NOVAFABRIC_PARENT_RUN_ID", raising=False)
    monkeypatch.delenv("NOVAFABRIC_DISTRIBUTION_ROLE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_RANK", raising=False)
    monkeypatch.delenv("WORLD_SIZE", raising=False)
    monkeypatch.delenv("SLURM_NTASKS", raising=False)
    monkeypatch.delenv("SLURM_ARRAY_TASK_COUNT", raising=False)
    monkeypatch.delenv("OMPI_COMM_WORLD_SIZE", raising=False)
    monkeypatch.delenv("RAY_WORLD_SIZE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_K8S_JOB_COMPLETIONS", raising=False)
    monkeypatch.delenv("NOVAFABRIC_FAIL_MODE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_PENDING_PARENT_TIMEOUT", raising=False)

    config = read_env()
    assert config.world_size == 16


def test_world_size_from_torchrun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-22: torchrun WORLD_SIZE env var."""
    monkeypatch.setenv("NOVAFABRIC_GLOBAL_RUN_ID", new_ulid())
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(tmp_path))
    monkeypatch.delenv("NOVAFABRIC_WORLD_SIZE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_PARENT_RUN_ID", raising=False)
    monkeypatch.delenv("NOVAFABRIC_DISTRIBUTION_ROLE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_RANK", raising=False)
    monkeypatch.setenv("WORLD_SIZE", "8")
    monkeypatch.delenv("SLURM_NTASKS", raising=False)
    monkeypatch.delenv("SLURM_ARRAY_TASK_COUNT", raising=False)
    monkeypatch.delenv("OMPI_COMM_WORLD_SIZE", raising=False)
    monkeypatch.delenv("RAY_WORLD_SIZE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_K8S_JOB_COMPLETIONS", raising=False)
    monkeypatch.delenv("NOVAFABRIC_FAIL_MODE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_PENDING_PARENT_TIMEOUT", raising=False)

    config = read_env()
    assert config.world_size == 8


def test_world_size_from_slurm_ntasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-22: Slurm srun SLURM_NTASKS env var."""
    monkeypatch.setenv("NOVAFABRIC_GLOBAL_RUN_ID", new_ulid())
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(tmp_path))
    monkeypatch.delenv("NOVAFABRIC_WORLD_SIZE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_PARENT_RUN_ID", raising=False)
    monkeypatch.delenv("NOVAFABRIC_DISTRIBUTION_ROLE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_RANK", raising=False)
    monkeypatch.delenv("WORLD_SIZE", raising=False)
    monkeypatch.setenv("SLURM_NTASKS", "32")
    monkeypatch.delenv("SLURM_ARRAY_TASK_COUNT", raising=False)
    monkeypatch.delenv("OMPI_COMM_WORLD_SIZE", raising=False)
    monkeypatch.delenv("RAY_WORLD_SIZE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_K8S_JOB_COMPLETIONS", raising=False)
    monkeypatch.delenv("NOVAFABRIC_FAIL_MODE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_PENDING_PARENT_TIMEOUT", raising=False)

    config = read_env()
    assert config.world_size == 32


def test_world_size_from_openmpi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-22: OpenMPI OMPI_COMM_WORLD_SIZE env var."""
    monkeypatch.setenv("NOVAFABRIC_GLOBAL_RUN_ID", new_ulid())
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(tmp_path))
    monkeypatch.delenv("NOVAFABRIC_WORLD_SIZE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_PARENT_RUN_ID", raising=False)
    monkeypatch.delenv("NOVAFABRIC_DISTRIBUTION_ROLE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_RANK", raising=False)
    monkeypatch.delenv("WORLD_SIZE", raising=False)
    monkeypatch.delenv("SLURM_NTASKS", raising=False)
    monkeypatch.delenv("SLURM_ARRAY_TASK_COUNT", raising=False)
    monkeypatch.setenv("OMPI_COMM_WORLD_SIZE", "4")
    monkeypatch.delenv("RAY_WORLD_SIZE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_K8S_JOB_COMPLETIONS", raising=False)
    monkeypatch.delenv("NOVAFABRIC_FAIL_MODE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_PENDING_PARENT_TIMEOUT", raising=False)

    config = read_env()
    assert config.world_size == 4


def test_world_size_none_when_not_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-23: world_size is null when no scheduler env var is set."""
    monkeypatch.setenv("NOVAFABRIC_GLOBAL_RUN_ID", new_ulid())
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(tmp_path))
    monkeypatch.delenv("NOVAFABRIC_WORLD_SIZE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_PARENT_RUN_ID", raising=False)
    monkeypatch.delenv("NOVAFABRIC_DISTRIBUTION_ROLE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_RANK", raising=False)
    monkeypatch.delenv("WORLD_SIZE", raising=False)
    monkeypatch.delenv("SLURM_NTASKS", raising=False)
    monkeypatch.delenv("SLURM_ARRAY_TASK_COUNT", raising=False)
    monkeypatch.delenv("OMPI_COMM_WORLD_SIZE", raising=False)
    monkeypatch.delenv("RAY_WORLD_SIZE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_K8S_JOB_COMPLETIONS", raising=False)
    monkeypatch.delenv("NOVAFABRIC_FAIL_MODE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_PENDING_PARENT_TIMEOUT", raising=False)

    config = read_env()
    assert config.world_size is None


def test_fail_mode_fail_closed_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-05: NOVAFABRIC_FAIL_MODE=fail-closed."""
    monkeypatch.setenv("NOVAFABRIC_GLOBAL_RUN_ID", new_ulid())
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(tmp_path))
    monkeypatch.setenv("NOVAFABRIC_FAIL_MODE", "fail-closed")
    monkeypatch.delenv("NOVAFABRIC_PARENT_RUN_ID", raising=False)
    monkeypatch.delenv("NOVAFABRIC_DISTRIBUTION_ROLE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_RANK", raising=False)
    monkeypatch.delenv("NOVAFABRIC_WORLD_SIZE", raising=False)
    monkeypatch.delenv("WORLD_SIZE", raising=False)
    monkeypatch.delenv("SLURM_NTASKS", raising=False)
    monkeypatch.delenv("SLURM_ARRAY_TASK_COUNT", raising=False)
    monkeypatch.delenv("OMPI_COMM_WORLD_SIZE", raising=False)
    monkeypatch.delenv("RAY_WORLD_SIZE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_K8S_JOB_COMPLETIONS", raising=False)
    monkeypatch.delenv("NOVAFABRIC_PENDING_PARENT_TIMEOUT", raising=False)

    config = read_env()
    from novafabric.capsule.schema import FailMode
    assert config.fail_mode == FailMode.fail_closed


def test_pending_parent_timeout_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVAFABRIC_GLOBAL_RUN_ID", new_ulid())
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(tmp_path))
    monkeypatch.setenv("NOVAFABRIC_PENDING_PARENT_TIMEOUT", "10.5")
    monkeypatch.delenv("NOVAFABRIC_PARENT_RUN_ID", raising=False)
    monkeypatch.delenv("NOVAFABRIC_DISTRIBUTION_ROLE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_RANK", raising=False)
    monkeypatch.delenv("NOVAFABRIC_WORLD_SIZE", raising=False)
    monkeypatch.delenv("WORLD_SIZE", raising=False)
    monkeypatch.delenv("SLURM_NTASKS", raising=False)
    monkeypatch.delenv("SLURM_ARRAY_TASK_COUNT", raising=False)
    monkeypatch.delenv("OMPI_COMM_WORLD_SIZE", raising=False)
    monkeypatch.delenv("RAY_WORLD_SIZE", raising=False)
    monkeypatch.delenv("NOVAFABRIC_K8S_JOB_COMPLETIONS", raising=False)
    monkeypatch.delenv("NOVAFABRIC_FAIL_MODE", raising=False)

    config = read_env()
    assert config.pending_parent_timeout_s == 10.5
