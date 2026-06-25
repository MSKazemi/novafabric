from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from novafabric.evals.adapter import EvalSuiteAdapter
from novafabric.evals.exceptions import EvalSuiteError
from novafabric.evals.result import EvalResult
from novafabric.evals.suites.swe_bench import SweBenchAdapter

_VALID_EVAL_RESULT_JSON = json.dumps(
    {
        "schema_version": "0.1.0",
        "suite_id": "swe-bench-verified-v1",
        "suite_version": "0.1.0",
        "oci_digest": "",
        "run_at": datetime.now(timezone.utc).isoformat(),
        "capsule_id": "test-capsule-002",
        "passed": True,
        "metrics": [
            {
                "name": "resolve_rate",
                "value": 0.72,
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
def adapter() -> SweBenchAdapter:
    return SweBenchAdapter()


def test_swe_bench_adapter_suite_id(adapter: SweBenchAdapter) -> None:
    assert adapter.suite_id() == "swe-bench-verified-v1"


def test_swe_bench_adapter_version(adapter: SweBenchAdapter) -> None:
    assert adapter.version() == "0.1.0"


def test_swe_bench_adapter_oci_digest_default_is_empty(
    adapter: SweBenchAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOVAFABRIC_SWE_BENCH_OCI_DIGEST", raising=False)
    assert adapter.oci_digest() == ""


def test_swe_bench_adapter_oci_digest_env_override(
    adapter: SweBenchAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = "sha256:" + "b" * 64
    monkeypatch.setenv("NOVAFABRIC_SWE_BENCH_OCI_DIGEST", fake)
    assert adapter.oci_digest() == fake


def test_swe_bench_adapter_oci_image_default(
    adapter: SweBenchAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOVAFABRIC_SWE_BENCH_OCI_IMAGE", raising=False)
    assert adapter.oci_image() == "ghcr.io/novafabric/swe-bench-eval:v1"


def test_swe_bench_adapter_oci_image_env_override(
    adapter: SweBenchAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "NOVAFABRIC_SWE_BENCH_OCI_IMAGE", "registry.example.com/my-swe:v2"
    )
    assert adapter.oci_image() == "registry.example.com/my-swe:v2"


def test_swe_bench_adapter_container_argv(adapter: SweBenchAdapter) -> None:
    argv = adapter.container_argv("/novafabric/eval-capsule")
    assert argv == ["swe-bench-run", "--capsule", "/novafabric/eval-capsule"]


def test_swe_bench_adapter_conforms_to_protocol(adapter: SweBenchAdapter) -> None:
    assert isinstance(adapter, EvalSuiteAdapter)


def test_swe_bench_run_delegates_to_docker_runner(
    tmp_path: Path, adapter: SweBenchAdapter
) -> None:
    """run() must call DockerRunner.run_eval_container with correct args."""
    with patch(
        "novafabric.evals.suites.swe_bench.DockerRunner"
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
    assert call_kwargs.kwargs["image_ref"] == "ghcr.io/novafabric/swe-bench-eval:v1"
    assert call_kwargs.kwargs["digest"] == adapter.oci_digest()
    assert call_kwargs.kwargs["capsule_path"] == tmp_path
    assert call_kwargs.kwargs["command"] == adapter.container_argv(str(tmp_path))
    assert isinstance(result, EvalResult)
    assert result.suite_id == "swe-bench-verified-v1"
    assert result.passed is True


def test_swe_bench_run_raises_eval_suite_error_on_nonzero_exit(
    tmp_path: Path, adapter: SweBenchAdapter
) -> None:
    """run() must raise EvalSuiteError when the container exits with a nonzero code."""
    with patch(
        "novafabric.evals.suites.swe_bench.DockerRunner"
    ) as MockDockerRunner:
        mock_instance = MagicMock()
        MockDockerRunner.return_value = mock_instance
        mock_instance.run_eval_container.return_value = (
            1,
            b"",
            b"container error output",
        )

        with pytest.raises(EvalSuiteError, match="swe-bench-verified-v1"):
            adapter.run(tmp_path, {})


def test_swe_bench_run_raises_eval_suite_error_on_invalid_json(
    tmp_path: Path, adapter: SweBenchAdapter
) -> None:
    """run() must raise EvalSuiteError when stdout is not valid EvalResult JSON."""
    with patch(
        "novafabric.evals.suites.swe_bench.DockerRunner"
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


def test_swe_bench_run_passes_config_as_env(
    tmp_path: Path, adapter: SweBenchAdapter
) -> None:
    """config dict is forwarded as the env parameter."""
    config = {"SWE_BENCH_SPLIT": "verified", "SWE_BENCH_LIMIT": "50"}
    with patch(
        "novafabric.evals.suites.swe_bench.DockerRunner"
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
