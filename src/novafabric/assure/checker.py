from __future__ import annotations

from pathlib import Path

import yaml

from novafabric._capsule_fs import read_capsule_member_or_none
from novafabric.assure.checks import ALL_CHECKS, BaseCheck
from novafabric.assure.models import AssuranceReport


class AssuranceChecker:
    def __init__(self, checks: list[BaseCheck] | None = None) -> None:
        self._checks = checks if checks is not None else ALL_CHECKS

    def check_all(self, capsule_dir: Path) -> AssuranceReport:
        manifest_path = capsule_dir / "capsule.yaml"
        run_id = "unknown"
        if manifest_path.exists():
            capsule_text = (
                read_capsule_member_or_none(manifest_path, operation="nova assure") or ""
            )
            m = yaml.safe_load(capsule_text) or {}
            run_id = m.get("run_id", "unknown")

        results = [check.run(capsule_dir) for check in self._checks]
        return AssuranceReport(
            run_id=run_id,
            capsule_path=str(capsule_dir),
            results=results,
        )
