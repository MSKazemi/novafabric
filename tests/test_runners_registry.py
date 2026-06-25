"""Tests for the runner registry (`get_runner`, `known_runner_names`)."""
from __future__ import annotations

import pytest

from novafabric.runners import (
    DockerRunner,
    LocalRunner,
    UnknownRunnerError,
    get_runner,
    known_runner_names,
)


def test_known_names_includes_built_ins() -> None:
    names = set(known_runner_names())
    assert "local" in names
    assert "docker" in names
    assert "kubernetes" in names
    assert "slurm" in names


def test_known_names_is_sorted() -> None:
    names = known_runner_names()
    assert names == sorted(names)


def test_get_local_returns_local_runner() -> None:
    runner = get_runner("local")
    assert isinstance(runner, LocalRunner)


def test_get_docker_returns_docker_runner() -> None:
    runner = get_runner("docker")
    assert isinstance(runner, DockerRunner)


def test_get_returns_fresh_instance_each_call() -> None:
    a = get_runner("local")
    b = get_runner("local")
    assert a is not b


def test_unknown_runner_raises_with_helpful_message() -> None:
    with pytest.raises(UnknownRunnerError) as excinfo:
        get_runner("not-a-real-runner")
    msg = str(excinfo.value)
    assert "not-a-real-runner" in msg
    assert "Available" in msg
    assert "local" in msg
    assert "docker" in msg
    assert "kubernetes" in msg
    assert "slurm" in msg


def test_get_kubernetes_returns_kubernetes_runner() -> None:
    from novafabric.runners import KubernetesRunner

    runner = get_runner("kubernetes")
    assert isinstance(runner, KubernetesRunner)


def test_get_slurm_returns_slurm_runner() -> None:
    from novafabric.runners import SlurmRunner

    runner = get_runner("slurm")
    assert isinstance(runner, SlurmRunner)
