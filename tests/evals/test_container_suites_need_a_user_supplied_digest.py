"""The container eval suites do not run on defaults (docs honesty).

No default eval image is published, and the adapters' default digest is empty.
``DockerRunner`` rejects an empty digest, so every container suite fails until the
operator supplies their own image and digest. ``docs/cli-reference.md`` states this;
this test pins the behaviour so the docs and the code cannot drift apart again
(they once claimed "default published digest" and a "host-env" fallback).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from novafabric.evals.suites.agent_bench import AgentBenchAdapter
from novafabric.evals.suites.gaia import GaiaAdapter
from novafabric.evals.suites.mmlu import MmluAdapter
from novafabric.evals.suites.swe_bench import SweBenchAdapter
from novafabric.evals.suites.truthful_qa import TruthfulQaAdapter
from novafabric.runners._types import ContainerEvalError

_SUITES = [
    (GaiaAdapter, "GAIA"),
    (AgentBenchAdapter, "AGENTBENCH"),
    (MmluAdapter, "MMLU"),
    (SweBenchAdapter, "SWE_BENCH"),
    (TruthfulQaAdapter, "TRUTHFUL_QA"),
]


@pytest.mark.parametrize(("adapter_cls", "var"), _SUITES)
def test_default_digest_is_empty_and_the_run_is_refused(adapter_cls, var, monkeypatch, tmp_path: Path):
    monkeypatch.delenv(f"NOVAFABRIC_{var}_OCI_DIGEST", raising=False)
    adapter = adapter_cls()
    assert adapter.oci_digest() == ""
    with pytest.raises(ContainerEvalError, match="invalid OCI digest"):
        adapter.run(tmp_path, {})


def test_cli_reference_does_not_claim_a_default_published_digest():
    text = (Path(__file__).resolve().parents[2] / "docs" / "cli-reference.md").read_text()
    assert "default published digest" not in text
