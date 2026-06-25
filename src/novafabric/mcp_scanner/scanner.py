from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from novafabric.mcp_scanner.models import RiskFinding, ScanReport, ToolRiskSummary
from novafabric.mcp_scanner.risk_rules import ALL_RULES, BaseRiskRule


class RiskScanner:
    def __init__(self, rules: list[BaseRiskRule] | None = None) -> None:
        self._rules = rules if rules is not None else ALL_RULES

    def scan(self, manifest: dict[str, Any]) -> ScanReport:
        server_name = manifest.get("name", "unknown")
        raw_tools: list[dict[str, Any]] = manifest.get("tools", [])
        tool_summaries: list[ToolRiskSummary] = []
        for tool in raw_tools:
            findings: list[RiskFinding] = []
            for rule in self._rules:
                findings.extend(rule.check(tool))
            tool_summaries.append(
                ToolRiskSummary(
                    tool_name=tool.get("name", "unknown"),
                    findings=findings,
                )
            )
        return ScanReport(
            server_name=server_name,
            tools=tool_summaries,
            metadata={
                "manifest_version": manifest.get("version", "unknown"),
                "tool_count": len(raw_tools),
                "rules_applied": len(self._rules),
            },
        )

    def scan_file(self, path: Path) -> ScanReport:
        text = path.read_text()
        if path.suffix in {".yaml", ".yml"}:
            manifest = yaml.safe_load(text)
        else:
            manifest = json.loads(text)
        return self.scan(manifest)
