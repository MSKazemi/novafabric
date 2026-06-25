from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from novafabric.evals.adapter import EvalSuiteAdapter
from novafabric.evals.exceptions import EvalSuiteError
from novafabric.evals.result import EvalResult
from novafabric.evals.suites.mmlu import MmluAdapter

_VALID_EVAL_RESULT_JSON = json.dumps(
    {
        "schema_version": "0.1.0",
        "suite_id": "mmlu-v1",
        "suite_version": "0.1.0",
        "oci_digest": "",
        "run_at": datetime.now(timezone.utc).isoformat(),
        "capsule_id": "test-capsule-001",
        "passed": True,
        "metrics": [
            {
                "name": "accuracy",
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
def adapter() -> MmluAdapter:
    return MmluAdapter()


def test_mmlu_adapter_suite_id(adapter: MmluAdapter) -> None:
    assert adapter.suite_id() == "mmlu-v1"


def test_mmlu_adapter_version(adapter: MmluAdapter) -> None:
    assert adapter.version() == "0.1.0"


def test_mmlu_adapter_oci_digest_default_is_empty(
    monkeypatch: pytest.MonkeyPatch, adapter: MmluAdapter
) -> None:
    monkeypatch.delenv("NOVAFABRIC_MMLU_OCI_DIGEST", raising=False)
    assert adapter.oci_digest() == ""


def test_mmlu_adapter_oci_digest_env_override(
    monkeypatch: pytest.MonkeyPatch, adapter: MmluAdapter
) -> None:
    monkeypatch.setenv("NOVAFABRIC_MMLU_OCI_DIGEST", "sha256:" + "a" * 64)
    assert adapter.oci_digest() == "sha256:" + "a" * 64


def test_mmlu_adapter_oci_image_default(
    monkeypatch: pytest.MonkeyPatch, adapter: MmluAdapter
) -> None:
    monkeypatch.delenv("NOVAFABRIC_MMLU_OCI_IMAGE", raising=False)
    assert adapter.oci_image() == "ghcr.io/novafabric/mmlu-eval:v1"


def test_mmlu_adapter_oci_image_env_override(
    monkeypatch: pytest.MonkeyPatch, adapter: MmluAdapter
) -> None:
    monkeypatch.setenv("NOVAFABRIC_MMLU_OCI_IMAGE", "registry.example.com/mmlu:custom")
    assert adapter.oci_image() == "registry.example.com/mmlu:custom"


def test_mmlu_adapter_container_argv(adapter: MmluAdapter) -> None:
    argv = adapter.container_argv("/novafabric/eval-capsule")
    assert argv == [
        "mmlu-run",
        "--capsule",
        "/novafabric/eval-capsule",
        "--output",
        "stdout",
    ]


def test_mmlu_adapter_conforms_to_protocol(adapter: MmluAdapter) -> None:
    assert isinstance(adapter, EvalSuiteAdapter)


def test_mmlu_run_delegates_to_docker_runner(
    tmp_path: Path, adapter: MmluAdapter
) -> None:
    """run() must call DockerRunner.run_eval_container with the correct image and digest."""
    with patch(
        "novafabric.evals.suites.mmlu.DockerRunner"
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
    assert call_kwargs.kwargs["image_ref"] == adapter.oci_image()
    assert call_kwargs.kwargs["digest"] == adapter.oci_digest()
    assert call_kwargs.kwargs["capsule_path"] == tmp_path
    assert call_kwargs.kwargs["command"] == adapter.container_argv(str(tmp_path))
    assert isinstance(result, EvalResult)
    assert result.suite_id == "mmlu-v1"
    assert result.passed is True


def test_mmlu_run_raises_eval_suite_error_on_nonzero_exit(
    tmp_path: Path, adapter: MmluAdapter
) -> None:
    """run() must raise EvalSuiteError when the container exits with a nonzero code."""
    with patch(
        "novafabric.evals.suites.mmlu.DockerRunner"
    ) as MockDockerRunner:
        mock_instance = MagicMock()
        MockDockerRunner.return_value = mock_instance
        mock_instance.run_eval_container.return_value = (
            1,
            b"",
            b"eval container error",
        )

        with pytest.raises(EvalSuiteError, match="mmlu-v1"):
            adapter.run(tmp_path, {})


def test_mmlu_run_raises_eval_suite_error_on_invalid_json(
    tmp_path: Path, adapter: MmluAdapter
) -> None:
    """run() must raise EvalSuiteError when stdout is not valid EvalResult JSON."""
    with patch(
        "novafabric.evals.suites.mmlu.DockerRunner"
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


def test_mmlu_run_passes_config_as_env(tmp_path: Path, adapter: MmluAdapter) -> None:
    """config dict is forwarded as the env parameter."""
    config = {"MMLU_SUBJECTS": "all", "MMLU_MAX_SAMPLES": "100"}
    with patch(
        "novafabric.evals.suites.mmlu.DockerRunner"
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
