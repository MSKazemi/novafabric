"""The container eval suites need a user-supplied image and digest (ADR-0308).

NovaFabric publishes no eval image, so the adapters carry no default image and no
default digest. ``run()`` refuses to start until both ``NOVAFABRIC_<SUITE>_OCI_IMAGE``
and ``..._OCI_DIGEST`` are set, and the error names the unset variable(s) before Docker
is touched. ``docs/cli-reference.md`` says the same; this test keeps docs and code from
drifting apart again (they once claimed "default published digest" and a "host-env"
fallback, and the placeholder ``ghcr.io/novafabric/<suite>-eval:v1`` 403s).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

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


_DIGEST = "sha256:" + "d" * 64
_IMAGE = "registry.example.com/eval:test"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for _, var in _SUITES:
        monkeypatch.delenv(f"NOVAFABRIC_{var}_OCI_IMAGE", raising=False)
        monkeypatch.delenv(f"NOVAFABRIC_{var}_OCI_DIGEST", raising=False)


@pytest.mark.parametrize(("adapter_cls", "var"), _SUITES)
def test_no_default_image_or_digest(adapter_cls, var):
    adapter = adapter_cls()
    assert adapter.oci_image() == ""
    assert adapter.oci_digest() == ""


@pytest.mark.parametrize(("adapter_cls", "var"), _SUITES)
def test_run_is_refused_naming_both_variables_before_docker(adapter_cls, var, tmp_path: Path):
    module = adapter_cls.__module__
    with patch(f"{module}.DockerRunner") as docker:
        with pytest.raises(ContainerEvalError) as exc:
            adapter_cls().run(tmp_path, {})
    docker.assert_not_called()
    assert f"NOVAFABRIC_{var}_OCI_IMAGE" in str(exc.value)
    assert f"NOVAFABRIC_{var}_OCI_DIGEST" in str(exc.value)


@pytest.mark.parametrize(("adapter_cls", "var"), _SUITES)
@pytest.mark.parametrize(("set_var", "missing_var"), [("IMAGE", "DIGEST"), ("DIGEST", "IMAGE")])
def test_run_names_only_the_variable_that_is_missing(
    adapter_cls, var, set_var, missing_var, monkeypatch, tmp_path: Path
):
    value = _IMAGE if set_var == "IMAGE" else _DIGEST
    monkeypatch.setenv(f"NOVAFABRIC_{var}_OCI_{set_var}", value)
    with patch(f"{adapter_cls.__module__}.DockerRunner"):
        with pytest.raises(ContainerEvalError) as exc:
            adapter_cls().run(tmp_path, {})
    message = str(exc.value)
    assert f"NOVAFABRIC_{var}_OCI_{missing_var} must be set" in message
    assert f"NOVAFABRIC_{var}_OCI_{set_var} and" not in message


def test_no_adapter_ships_a_placeholder_registry_reference():
    suites = Path(__file__).resolve().parents[2] / "src" / "novafabric" / "evals" / "suites"
    offenders = [p.name for p in suites.glob("*.py") if "ghcr.io/novafabric" in p.read_text()]
    assert offenders == []


def test_cli_reference_does_not_claim_a_default_published_digest():
    text = (Path(__file__).resolve().parents[2] / "docs" / "cli-reference.md").read_text()
    assert "default published digest" not in text
