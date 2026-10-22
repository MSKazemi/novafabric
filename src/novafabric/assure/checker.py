from __future__ import annotations

from pathlib import Path

import yaml

from novafabric._capsule_fs import read_required_capsule_member
from novafabric.assure.checks import ALL_CHECKS, BaseCheck
from novafabric.assure.models import AssuranceReport


class AssuranceChecker:
    def __init__(self, checks: list[BaseCheck] | None = None) -> None:
        self._checks = checks if checks is not None else ALL_CHECKS

    def check_all(self, capsule_dir: Path) -> AssuranceReport:
        run_id = "unknown"
        # Present but unusable (a link, FIFO, directory): CapsuleRequiredFileError.
        capsule_text = read_required_capsule_member(
            capsule_dir / "capsule.yaml", operation="nova assure"
        )
        if capsule_text is not None:
            m = yaml.safe_load(capsule_text) or {}
            run_id = m.get("run_id", "unknown")

        results = [check.run(capsule_dir) for check in self._checks]
        return AssuranceReport(
            run_id=run_id,
            capsule_path=str(capsule_dir),
            results=results,
        )
