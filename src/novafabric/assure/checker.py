from __future__ import annotations

from pathlib import Path

import yaml

from novafabric.assure.checks import ALL_CHECKS, BaseCheck
from novafabric.assure.models import AssuranceReport


class AssuranceChecker:
    def __init__(self, checks: list[BaseCheck] | None = None) -> None:
        self._checks = checks if checks is not None else ALL_CHECKS

    def check_all(self, capsule_dir: Path) -> AssuranceReport:
        manifest_path = capsule_dir / "capsule.yaml"
        run_id = "unknown"
        if manifest_path.exists():
            m = yaml.safe_load(manifest_path.read_text()) or {}
            run_id = m.get("run_id", "unknown")

        results = [check.run(capsule_dir) for check in self._checks]
        return AssuranceReport(
            run_id=run_id,
            capsule_path=str(capsule_dir),
            results=results,
        )
