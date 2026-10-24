"""Every container suite tells its binary where the capsule is *inside* the container.

Review eval-adapters-query-08: the five suites passed the host path
(``/home/<user>/...``) to ``container_argv``, documented as "the path inside the
container", while ``DockerRunner.run_eval_container`` mounts the capsule at
``EVAL_CAPSULE_MOUNTPOINT``. The suite binary was pointed at a path that does not
exist in the container. One property over all five implementations, asserted once.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from novafabric.evals.result import EvalResult, Metric
from novafabric.evals.suites.agent_bench import AgentBenchAdapter
from novafabric.evals.suites.gaia import GaiaAdapter
from novafabric.evals.suites.mmlu import MmluAdapter
from novafabric.evals.suites.swe_bench import SweBenchAdapter
from novafabric.evals.suites.truthful_qa import TruthfulQaAdapter
from novafabric.runners._docker import EVAL_CAPSULE_MOUNTPOINT, DockerRunner

_SUITES = [
    (GaiaAdapter, "GAIA"),
    (AgentBenchAdapter, "AGENTBENCH"),
    (MmluAdapter, "MMLU"),
    (SweBenchAdapter, "SWE_BENCH"),
    (TruthfulQaAdapter, "TRUTHFUL_QA"),
]


def _result_json() -> bytes:
    return EvalResult(
        suite_id="s",
        suite_version="1",
        oci_digest="sha256:" + "d" * 64,
        run_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        capsule_id="c",
        passed=True,
        metrics=[Metric(name="m", value=1.0, unit="score", threshold=0.5, passed=True)],
    ).model_dump_json().encode()


def test_the_runner_mounts_the_capsule_at_the_public_mount_point() -> None:
    assert DockerRunner._EVAL_CAPSULE_MOUNTPOINT == EVAL_CAPSULE_MOUNTPOINT


@pytest.mark.parametrize(("adapter_cls", "var"), _SUITES)
def test_the_container_argv_names_the_mount_point_not_the_host_path(
    adapter_cls, var: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(f"NOVAFABRIC_{var}_OCI_IMAGE", "registry.example.com/eval:test")
    monkeypatch.setenv(f"NOVAFABRIC_{var}_OCI_DIGEST", "sha256:" + "d" * 64)
    capsule = tmp_path / "host-capsule"
    capsule.mkdir()
    with patch.object(
        DockerRunner, "run_eval_container", return_value=(0, _result_json(), b"")
    ) as run:
        adapter_cls().run(capsule, {})
    kwargs = run.call_args.kwargs
    assert kwargs["capsule_path"] == capsule  # what gets mounted
    assert EVAL_CAPSULE_MOUNTPOINT in kwargs["command"]  # what the binary is told
    assert not any(str(capsule) in arg for arg in kwargs["command"])
