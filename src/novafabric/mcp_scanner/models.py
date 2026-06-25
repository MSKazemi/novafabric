from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, computed_field


class OWASPCategory(str, Enum):
    LLM01 = "LLM01-PromptInjection"
    LLM03 = "LLM03-SupplyChain"
    LLM05 = "LLM05-OutputHandling"
    LLM06 = "LLM06-ExcessiveAgency"


class RiskSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"

    @property
    def score(self) -> int:
        return {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0}[self.value]


class RiskFinding(BaseModel):
    category: OWASPCategory
    severity: RiskSeverity
    rule_id: str
    message: str
    evidence: str
    tool_name: str


class ToolRiskSummary(BaseModel):
    tool_name: str
    findings: list[RiskFinding] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def risk_score(self) -> float:
        if not self.findings:
            return 0.0
        return sum(f.severity.score for f in self.findings) / len(self.findings)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def highest_severity(self) -> str:
        if not self.findings:
            return "NONE"
        order = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
        for sev in order:
            if any(f.severity.value == sev for f in self.findings):
                return sev
        return "NONE"


class ScanReport(BaseModel):
    server_name: str
    tools: list[ToolRiskSummary] = []
    metadata: dict[str, Any] = {}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_findings(self) -> int:
        return sum(len(t.findings) for t in self.tools)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def overall_risk_level(self) -> str:
        if not self.tools:
            return "LOW"
        order = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
        for level in order:
            if any(t.highest_severity == level for t in self.tools):
                return level
        return "LOW"

    def model_dump_report(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
