"""MMLU (Massive Multitask Language Understanding) eval suite adapter (ADR-0033).

Evaluates LLM performance across 57 academic subject areas.  Delegates to an
OCI-pinned container via
:class:`~novafabric.runners._docker.DockerRunner` and parses the container's
stdout as :class:`~novafabric.evals.result.EvalResult` JSON.

Set NOVAFABRIC_MMLU_OCI_DIGEST / NOVAFABRIC_MMLU_OCI_IMAGE env vars for
production deployment.
"""
from __future__ import annotations

import os
from pathlib import Path

from novafabric.evals.exceptions import EvalSuiteError
from novafabric.evals.result import EvalResult
from novafabric.runners._docker import DockerRunner

_SUITE_ID = "mmlu-v1"
_SUITE_VERSION = "0.1.0"
_DEFAULT_OCI_IMAGE = "ghcr.io/novafabric/mmlu-eval:v1"
_DEFAULT_OCI_DIGEST = ""


class MmluAdapter:
    """Adapter for the MMLU (Massive Multitask Language Understanding) benchmark.

    Runs the benchmark inside an OCI-pinned container via
    :class:`~novafabric.runners._docker.DockerRunner`.  The container is
    expected to write an :class:`~novafabric.evals.result.EvalResult`
    JSON object to stdout upon completion.
    """

    def suite_id(self) -> str:
        """Return the stable identifier for this suite."""
        return _SUITE_ID

    def version(self) -> str:
        """Return the semver of this adapter implementation."""
        return _SUITE_VERSION

    def oci_digest(self) -> str:
        """Return the pinned OCI image digest for the MMLU eval container.

        Reads ``NOVAFABRIC_MMLU_OCI_DIGEST`` from the environment.  Returns
        ``""`` (the unpinned default) when the variable is not set.
        """
        return os.environ.get("NOVAFABRIC_MMLU_OCI_DIGEST", _DEFAULT_OCI_DIGEST)

    def oci_image(self) -> str:
        """Return the OCI image reference for the MMLU eval container.

        Reads ``NOVAFABRIC_MMLU_OCI_IMAGE`` from the environment.  Returns
        :data:`_DEFAULT_OCI_IMAGE` when the variable is not set.
        """
        return os.environ.get("NOVAFABRIC_MMLU_OCI_IMAGE", _DEFAULT_OCI_IMAGE)

    def container_argv(self, capsule_mount: str) -> list[str]:
        """Return the argv to execute inside the MMLU eval container.

        Parameters
        ----------
        capsule_mount:
            The path inside the container where the capsule is mounted.
        """
        return ["mmlu-run", "--capsule", capsule_mount, "--output", "stdout"]

    def run(self, capsule_path: Path, config: dict[str, str]) -> EvalResult:
        """Execute the MMLU suite against *capsule_path* and return a result.

        Delegates to
        :meth:`~novafabric.runners._docker.DockerRunner.run_eval_container`.
        The container's stdout is parsed as
        :class:`~novafabric.evals.result.EvalResult` JSON.

        Parameters
        ----------
        capsule_path:
            Path to the capsule directory on the host.
        config:
            Adapter-specific key=value pairs forwarded as container env vars.

        Raises
        ------
        EvalSuiteError
            If the container exits with a nonzero return code or if the
            stdout cannot be parsed as a valid :class:`EvalResult`.
        """
        runner = DockerRunner()
        returncode, stdout, stderr = runner.run_eval_container(
            image_ref=self.oci_image(),
            digest=self.oci_digest(),
            capsule_path=capsule_path,
            command=self.container_argv(str(capsule_path)),
            env=config,
        )
        if returncode != 0:
            stderr_text = stderr.decode(errors="replace").strip()
            raise EvalSuiteError(
                f"{_SUITE_ID}: eval container exited with code {returncode}. "
                f"stderr: {stderr_text}"
            )
        try:
            return EvalResult.model_validate_json(stdout)
        except Exception as exc:
            raise EvalSuiteError(
                f"{_SUITE_ID}: failed to parse EvalResult from stdout: {exc}"
            ) from exc
