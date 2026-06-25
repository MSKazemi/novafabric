from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from novafabric.evals.result import EvalResult, Metric

_SUITE_ID = "novafabric-smoke-v1"
_SUITE_VERSION = "0.1.0"


class SmokeAdapter:
    """Built-in smoke suite — runs entirely in host Python, no container.

    Checks two invariants on the capsule directory:

    1. ``capsule.yaml`` exists and is valid YAML containing a ``run_id`` field.
    2. ``replay-policy.yaml`` exists.

    The OCI digest is the empty string because no container is required.
    """

    def suite_id(self) -> str:
        return _SUITE_ID

    def version(self) -> str:
        return _SUITE_VERSION

    def oci_digest(self) -> str:
        return ""

    def run(self, capsule_path: Path, config: dict[str, str]) -> EvalResult:  # noqa: ARG002
        metrics: list[Metric] = []
        capsule_id = "unknown"

        # ── Metric 1: capsule.yaml exists and has run_id ──────────────────────
        capsule_yaml_path = capsule_path / "capsule.yaml"
        capsule_schema_passed = False
        if capsule_yaml_path.exists():
            try:
                data = yaml.safe_load(capsule_yaml_path.read_text())
                if isinstance(data, dict) and "run_id" in data:
                    capsule_schema_passed = True
                    capsule_id = str(data["run_id"])
            except yaml.YAMLError:
                pass

        metrics.append(
            Metric(
                name="capsule_schema",
                value=1.0 if capsule_schema_passed else 0.0,
                unit="score",
                threshold=1.0,
                passed=capsule_schema_passed,
            )
        )

        # ── Metric 2: replay-policy.yaml exists ───────────────────────────────
        replay_policy_path = capsule_path / "replay-policy.yaml"
        replay_policy_passed = replay_policy_path.exists()
        metrics.append(
            Metric(
                name="replay_policy_present",
                value=1.0 if replay_policy_passed else 0.0,
                unit="score",
                threshold=1.0,
                passed=replay_policy_passed,
            )
        )

        overall_passed = all(m.passed for m in metrics)

        return EvalResult(
            schema_version="0.1.0",
            suite_id=_SUITE_ID,
            suite_version=_SUITE_VERSION,
            oci_digest="",
            run_at=datetime.now(timezone.utc),
            capsule_id=capsule_id,
            passed=overall_passed,
            metrics=metrics,
            statistical_context=None,
            notes=None,
        )
