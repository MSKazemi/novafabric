"""Runner registry — name → class lookup (ADR-0025).

Centralizes the ``--runner <name>`` resolution so the CLI does not need
to know which concrete runners exist. New built-in runners register
here. Third-party runners (deferred per ADR-0025 §Alternatives — plugin
discovery) would register via an entry-point group in a future revision.
"""
from __future__ import annotations

from typing import Callable

from novafabric.runners._docker import DockerRunner
from novafabric.runners._kubernetes import KubernetesRunner
from novafabric.runners._local import LocalRunner
from novafabric.runners._lsf import LSFRunner
from novafabric.runners._pbs import PBSRunner
from novafabric.runners._slurm import SlurmRunner
from novafabric.runners._types import RunnerSpec


class UnknownRunnerError(ValueError):
    """Raised when ``--runner <name>`` does not match a known runner."""


_BUILTIN_RUNNERS: dict[str, Callable[[], RunnerSpec]] = {
    "local": lambda: LocalRunner(),
    "docker": lambda: DockerRunner(),
    "kubernetes": lambda: KubernetesRunner(),
    "lsf": lambda: LSFRunner(),
    "pbs": lambda: PBSRunner(),
    "slurm": lambda: SlurmRunner(),
}


def known_runner_names() -> list[str]:
    return sorted(_BUILTIN_RUNNERS.keys())


def get_runner(name: str) -> RunnerSpec:
    """Resolve ``name`` to a fresh runner instance.

    Raises :class:`UnknownRunnerError` with a helpful message listing
    available runners if ``name`` is not recognized.
    """
    factory = _BUILTIN_RUNNERS.get(name)
    if factory is None:
        raise UnknownRunnerError(
            f"unknown runner: {name!r}. Available: {', '.join(known_runner_names())}"
        )
    return factory()
