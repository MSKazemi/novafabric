"""Runner abstraction for multi-target capture (ADR-0025).

Public API:

- :class:`RunnerSpec` — protocol every runner satisfies
- :class:`RunnerJobSpec` — what the orchestrator hands to a runner
- :class:`RunnerJobResult` — what the runner hands back
- :class:`LocalRunner` — the v0.5-and-earlier behavior, now behind the
  protocol (Runners.1)

Future runners (`DockerRunner`, `KubernetesRunner`, `SlurmRunner`) live in
sibling files and satisfy the same protocol. See
the private ``design/adr/0025-runner-spec-interface.md``
for the design.
"""
from __future__ import annotations

from novafabric.runners._docker import DockerRunner
from novafabric.runners._kubernetes import KubernetesRunner
from novafabric.runners._local import LocalRunner
from novafabric.runners._lsf import LSFRunner
from novafabric.runners._pbs import PBSRunner
from novafabric.runners._registry import (
    UnknownRunnerError,
    get_runner,
    known_runner_names,
)
from novafabric.runners._slurm import SlurmRunner
from novafabric.runners._types import (
    ContainerEvalError,
    RunnerJobResult,
    RunnerJobSpec,
    RunnerSpec,
)

__all__ = [
    "ContainerEvalError",
    "DockerRunner",
    "KubernetesRunner",
    "LSFRunner",
    "LocalRunner",
    "PBSRunner",
    "RunnerJobResult",
    "RunnerJobSpec",
    "RunnerSpec",
    "SlurmRunner",
    "UnknownRunnerError",
    "get_runner",
    "known_runner_names",
]
