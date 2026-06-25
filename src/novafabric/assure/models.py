from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, computed_field


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"


class AssuranceResult(BaseModel):
    check_id: str
    category: str
    status: CheckStatus
    message: str
    evidence: dict[str, Any] = {}


class AssuranceReport(BaseModel):
    run_id: str
    capsule_path: str
    results: list[AssuranceResult] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.results if r.status == CheckStatus.PASS)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if r.status == CheckStatus.FAIL)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def warn_count(self) -> int:
        return sum(1 for r in self.results if r.status == CheckStatus.WARN)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def skip_count(self) -> int:
        return sum(1 for r in self.results if r.status == CheckStatus.SKIP)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def overall_status(self) -> CheckStatus:
        if any(r.status == CheckStatus.FAIL for r in self.results):
            return CheckStatus.FAIL
        if any(r.status == CheckStatus.WARN for r in self.results):
            return CheckStatus.WARN
        return CheckStatus.PASS
