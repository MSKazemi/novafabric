from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from novafabric.evals.result import EvalResult


@runtime_checkable
class EvalSuiteAdapter(Protocol):
    """Protocol that every eval suite adapter must implement.

    Adapters are discovered via the ``novafabric.eval_suites`` entry-point
    group.  The entry-point name is arbitrary; the suite is identified by the
    value returned from :meth:`suite_id`.

    Host-env adapters (no container) return an empty string from
    :meth:`oci_digest`.  OCI-based adapters return the full
    ``sha256:<hex>`` digest of the image they ran in.
    """

    def suite_id(self) -> str:
        """Stable identifier for this suite (e.g. ``novafabric-smoke-v1``)."""
        ...

    def version(self) -> str:
        """Semver of this adapter implementation."""
        ...

    def oci_digest(self) -> str:
        """OCI image digest or ``""`` for host-env adapters."""
        ...

    def run(self, capsule_path: Path, config: dict[str, str]) -> EvalResult:
        """Execute the suite against *capsule_path* and return a result.

        Parameters
        ----------
        capsule_path:
            Path to the capsule directory (contains ``capsule.yaml``).
        config:
            Adapter-specific key=value pairs passed from the CLI
            ``--config`` option.
        """
        ...
