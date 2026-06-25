# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""NOVAFABRIC_* env-var contract reader + per-scheduler world_size mapping (cap-004, cap-006).

FR-18: Identity via env-var contract:
  NOVAFABRIC_GLOBAL_RUN_ID    — propagated by submitter SDK at submission time
  NOVAFABRIC_PARENT_RUN_ID    — set by parent process for child workers
  NOVAFABRIC_CAPSULE_DIR      — local spool directory
  NOVAFABRIC_RANK             — worker rank (0-indexed)
  NOVAFABRIC_WORLD_SIZE       — total number of workers
  NOVAFABRIC_DISTRIBUTION_ROLE — DRIVER | WORKER
  NOVAFABRIC_FAIL_MODE        — fail-open | fail-closed
  NOVAFABRIC_PENDING_PARENT_TIMEOUT — seconds (float), default 86400

FR-22: Per-scheduler world_size mapping:
  Slurm srun     → SLURM_NTASKS
  Slurm sbatch   → SLURM_ARRAY_TASK_COUNT
  K8s Job        → NOVAFABRIC_K8S_JOB_COMPLETIONS (injected by operator)
  torchrun       → WORLD_SIZE
  OpenMPI        → OMPI_COMM_WORLD_SIZE
  Ray Train      → RAY_WORLD_SIZE (injected by our Ray Train wrapper)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from novafabric.capsule.schema import CapsuleRole, DistributionRole, FailMode
from novafabric.capsule.ulid_util import new_ulid

logger = logging.getLogger(__name__)

# ── Scheduler world_size probe order ────────────────────────────────────
# Each entry: (scheduler_name, env_var)
SCHEDULER_WORLD_SIZE_VARS: list[tuple[str, str]] = [
    ("torchrun", "WORLD_SIZE"),
    ("slurm_srun", "SLURM_NTASKS"),
    ("slurm_sbatch", "SLURM_ARRAY_TASK_COUNT"),
    ("openmpi", "OMPI_COMM_WORLD_SIZE"),
    ("ray_train", "RAY_WORLD_SIZE"),
    ("k8s_job", "NOVAFABRIC_K8S_JOB_COMPLETIONS"),
]


def _probe_world_size() -> int | None:
    """Probe scheduler env vars in priority order; return first valid int or None."""
    for _scheduler, var in SCHEDULER_WORLD_SIZE_VARS:
        raw = os.environ.get(var)
        if raw:
            try:
                val = int(raw)
                if val > 0:
                    return val
            except ValueError:
                logger.debug("Ignoring non-int %s=%r", var, raw)
    return None


# ── Dataclass for resolved env config ────────────────────────────────────


@dataclass
class CapsuleEnvConfig:
    """Resolved env-var configuration for a single capsule writer instance."""

    global_run_id: str
    parent_run_id: str | None
    capsule_dir: str
    rank: int | None
    world_size: int | None
    distribution_role: DistributionRole | None
    capsule_role: CapsuleRole
    fail_mode: FailMode
    pending_parent_timeout_s: float
    warnings: list[str] = field(default_factory=list)


def read_env() -> CapsuleEnvConfig:
    """Read and resolve the NOVAFABRIC_* env-var contract (FR-18, FR-20).

    Graceful fallback when env vars absent (FR-20):
      - capsule_role defaults to STANDALONE
      - synthesises a placeholder global_run_id
      - records a warning for `nova doctor`
    """
    warnings: list[str] = []

    # global_run_id — generated once at submitter SDK time (FR-28)
    global_run_id = os.environ.get("NOVAFABRIC_GLOBAL_RUN_ID", "")
    if not global_run_id:
        global_run_id = new_ulid()
        warnings.append(
            "NOVAFABRIC_GLOBAL_RUN_ID not set; synthesised placeholder "
            f"global_run_id={global_run_id!r}. "
            "Run `nova doctor` for details."
        )

    parent_run_id = os.environ.get("NOVAFABRIC_PARENT_RUN_ID") or None

    capsule_dir = os.environ.get("NOVAFABRIC_CAPSULE_DIR", ".")
    if not os.path.isabs(capsule_dir):
        warnings.append(
            f"NOVAFABRIC_CAPSULE_DIR={capsule_dir!r} is relative — "
            "absolute paths are recommended for reliability."
        )

    # Rank
    rank: int | None = None
    raw_rank = os.environ.get("NOVAFABRIC_RANK")
    if raw_rank:
        try:
            rank = int(raw_rank)
        except ValueError:
            warnings.append(f"NOVAFABRIC_RANK={raw_rank!r} is not an integer; ignoring.")

    # World size — NOVAFABRIC_WORLD_SIZE takes precedence; then scheduler probing
    world_size: int | None = None
    raw_ws = os.environ.get("NOVAFABRIC_WORLD_SIZE")
    if raw_ws:
        try:
            world_size = int(raw_ws)
        except ValueError:
            warnings.append(
                f"NOVAFABRIC_WORLD_SIZE={raw_ws!r} is not an integer; ignoring."
            )
    if world_size is None:
        world_size = _probe_world_size()

    # Distribution role
    distribution_role: DistributionRole | None = None
    raw_role = os.environ.get("NOVAFABRIC_DISTRIBUTION_ROLE", "")
    if raw_role:
        try:
            distribution_role = DistributionRole(raw_role.upper())
        except ValueError:
            warnings.append(
                f"NOVAFABRIC_DISTRIBUTION_ROLE={raw_role!r} is not DRIVER|WORKER; ignoring."
            )

    # Capsule role — derived from presence of parent_run_id
    capsule_role: CapsuleRole
    if parent_run_id:
        capsule_role = CapsuleRole.CHILD
    elif world_size and world_size > 1:
        capsule_role = CapsuleRole.PARENT
    else:
        capsule_role = CapsuleRole.STANDALONE

    # Fail mode
    fail_mode = FailMode.fail_open
    raw_fail = os.environ.get("NOVAFABRIC_FAIL_MODE", "fail-open")
    try:
        fail_mode = FailMode(raw_fail)
    except ValueError:
        warnings.append(
            f"NOVAFABRIC_FAIL_MODE={raw_fail!r} is not fail-open|fail-closed; "
            "defaulting to fail-open."
        )

    # Pending parent timeout
    pending_parent_timeout_s = 86400.0
    raw_timeout = os.environ.get("NOVAFABRIC_PENDING_PARENT_TIMEOUT")
    if raw_timeout:
        try:
            pending_parent_timeout_s = float(raw_timeout)
        except ValueError:
            warnings.append(
                f"NOVAFABRIC_PENDING_PARENT_TIMEOUT={raw_timeout!r} is not a float; "
                "defaulting to 86400."
            )

    for w in warnings:
        logger.warning("novafabric.env_contract: %s", w)

    return CapsuleEnvConfig(
        global_run_id=global_run_id,
        parent_run_id=parent_run_id,
        capsule_dir=capsule_dir,
        rank=rank,
        world_size=world_size,
        distribution_role=distribution_role,
        capsule_role=capsule_role,
        fail_mode=fail_mode,
        pending_parent_timeout_s=pending_parent_timeout_s,
        warnings=warnings,
    )
