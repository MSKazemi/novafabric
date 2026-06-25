from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from novafabric.evals.adapter import EvalSuiteAdapter
from novafabric.evals.exceptions import EvalSuiteError
from novafabric.evals.result import EvalResult
from novafabric.evals.suites.agent_bench import AgentBenchAdapter

_VALID_EVAL_RESULT_JSON = json.dumps(
    {
        "schema_version": "0.1.0",
        "suite_id": "agentbench-v1",
        "suite_version": "0.1.0",
        "oci_digest": "",
        "run_at": datetime.now(timezone.utc).isoformat(),
        "capsule_id": "test-capsule-003",
        "passed": True,
        "metrics": [
            {
                "name": "overall_score",
                "value": 0.68,
                "unit": "score",
                "threshold": 0.5,
                "passed": True,
            }
        ],
        "statistical_context": None,
        "notes": None,
    }
)


@pytest.fixture
def adapter() -> AgentBenchAdapter:
    return AgentBenchAdapter()


def test_agent_bench_adapter_suite_id(adapter: AgentBenchAdapter) -> None:
    assert adapter.suite_id() == "agentbench-v1"


def test_agent_bench_adapter_version(adapter: AgentBenchAdapter) -> None:
    assert adapter.version() == "0.1.0"


def test_agent_bench_adapter_oci_digest_default_is_empty(
    adapter: AgentBenchAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOVAFABRIC_AGENTBENCH_OCI_DIGEST", raising=False)
    assert adapter.oci_digest() == ""


def test_agent_bench_adapter_oci_digest_env_override(
    adapter: AgentBenchAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = "sha256:" + "c" * 64
    monkeypatch.setenv("NOVAFABRIC_AGENTBENCH_OCI_DIGEST", fake)
    assert adapter.oci_digest() == fake


def test_agent_bench_adapter_oci_image_default(
    adapter: AgentBenchAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOVAFABRIC_AGENTBENCH_OCI_IMAGE", raising=False)
    assert adapter.oci_image() == "ghcr.io/novafabric/agentbench-eval:v1"


def test_agent_bench_adapter_oci_image_env_override(
    adapter: AgentBenchAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "NOVAFABRIC_AGENTBENCH_OCI_IMAGE", "registry.example.com/my-agentbench:v2"
    )
    assert adapter.oci_image() == "registry.example.com/my-agentbench:v2"


def test_agent_bench_adapter_container_argv(adapter: AgentBenchAdapter) -> None:
    argv = adapter.container_argv("/novafabric/eval-capsule")
    assert argv == ["agentbench-run", "--capsule", "/novafabric/eval-capsule"]


def test_agent_bench_adapter_conforms_to_protocol(adapter: AgentBenchAdapter) -> None:
    assert isinstance(adapter, EvalSuiteAdapter)


def test_agent_bench_run_delegates_to_docker_runner(
    tmp_path: Path, adapter: AgentBenchAdapter
) -> None:
    """run() must call DockerRunner.run_eval_container with correct args."""
    with patch(
        "novafabric.evals.suites.agent_bench.DockerRunner"
    ) as MockDockerRunner:
        mock_instance = MagicMock()
        MockDockerRunner.return_value = mock_instance
        mock_instance.run_eval_container.return_value = (
            0,
            _VALID_EVAL_RESULT_JSON.encode(),
            b"",
        )

        result = adapter.run(tmp_path, {})

    MockDockerRunner.assert_called_once_with()
    call_kwargs = mock_instance.run_eval_container.call_args
    assert call_kwargs.kwargs["image_ref"] == "ghcr.io/novafabric/agentbench-eval:v1"
    assert call_kwargs.kwargs["digest"] == adapter.oci_digest()
    assert call_kwargs.kwargs["capsule_path"] == tmp_path
    assert call_kwargs.kwargs["command"] == adapter.container_argv(str(tmp_path))
    assert isinstance(result, EvalResult)
    assert result.suite_id == "agentbench-v1"
    assert result.passed is True


def test_agent_bench_run_raises_eval_suite_error_on_nonzero_exit(
    tmp_path: Path, adapter: AgentBenchAdapter
) -> None:
    """run() must raise EvalSuiteError when the container exits with a nonzero code."""
    with patch(
        "novafabric.evals.suites.agent_bench.DockerRunner"
    ) as MockDockerRunner:
        mock_instance = MagicMock()
        MockDockerRunner.return_value = mock_instance
        mock_instance.run_eval_container.return_value = (
            2,
            b"",
            b"agentbench setup failure",
        )

        with pytest.raises(EvalSuiteError, match="agentbench-v1"):
            adapter.run(tmp_path, {})


def test_agent_bench_run_raises_eval_suite_error_on_invalid_json(
    tmp_path: Path, adapter: AgentBenchAdapter
) -> None:
    """run() must raise EvalSuiteError when stdout is not valid EvalResult JSON."""
    with patch(
        "novafabric.evals.suites.agent_bench.DockerRunner"
    ) as MockDockerRunner:
        mock_instance = MagicMock()
        MockDockerRunner.return_value = mock_instance
        mock_instance.run_eval_container.return_value = (
            0,
            b"not valid json at all",
            b"",
        )

        with pytest.raises(EvalSuiteError, match="failed to parse EvalResult"):
            adapter.run(tmp_path, {})


def test_agent_bench_run_passes_config_as_env(
    tmp_path: Path, adapter: AgentBenchAdapter
) -> None:
    """config dict is forwarded as the env parameter."""
    config = {"AGENTBENCH_TASKS": "os,db,kg", "AGENTBENCH_MAX_ROUNDS": "5"}
    with patch(
        "novafabric.evals.suites.agent_bench.DockerRunner"
    ) as MockDockerRunner:
        mock_instance = MagicMock()
        MockDockerRunner.return_value = mock_instance
        mock_instance.run_eval_container.return_value = (
            0,
            _VALID_EVAL_RESULT_JSON.encode(),
            b"",
        )

        adapter.run(tmp_path, config)

    call_kwargs = mock_instance.run_eval_container.call_args
    assert call_kwargs.kwargs["env"] == config
