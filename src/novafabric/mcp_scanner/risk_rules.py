from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any

from novafabric.mcp_scanner.models import OWASPCategory, RiskFinding, RiskSeverity

_PROMPT_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"disregard\s+(all\s+)?prior\s+instructions",
    r"forget\s+(all\s+)?previous",
    r"you\s+are\s+now\s+a",
    r"act\s+as\s+(a\s+)?(?:different|new|another)\s+",
    r"system\s+prompt",
    r"new\s+persona",
    r"bypass\s+(all\s+)?restrictions",
    r"jailbreak",
]

_EXCESSIVE_AGENCY_KEYWORDS = [
    "delete",
    "remove",
    "drop",
    "purge",
    "erase",
    r"send\s+email",
    r"send\s+message",
    r"post\s+to",
    r"modify\s+system",
    r"system\s+settings",
    r"write\s+to\s+disk",
    "execute",
    r"run\s+command",
    "shell",
    "subprocess",
    "elevate",
    "privilege",
    "sudo",
    "admin",
    "deploy",
    r"push\s+to\s+production",
]


class BaseRiskRule(ABC):
    @abstractmethod
    def check(self, tool: dict[str, Any]) -> list[RiskFinding]:
        ...


class LLM01PromptInjectionRule(BaseRiskRule):
    def check(self, tool: dict[str, Any]) -> list[RiskFinding]:
        findings: list[RiskFinding] = []
        text = f"{tool.get('name', '')} {tool.get('description', '')}".lower()
        for pattern in _PROMPT_INJECTION_PATTERNS:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                findings.append(
                    RiskFinding(
                        category=OWASPCategory.LLM01,
                        severity=RiskSeverity.HIGH,
                        rule_id="LLM01-001",
                        message="Tool text contains potential prompt injection pattern",
                        evidence=m.group(0),
                        tool_name=tool.get("name", "unknown"),
                    )
                )
                break
        return findings


class LLM03SupplyChainRule(BaseRiskRule):
    def check(self, tool: dict[str, Any]) -> list[RiskFinding]:
        findings: list[RiskFinding] = []
        schema_str = str(tool.get("inputSchema", {}))
        # detect unversioned/cleartext external URL references in schema defaults
        url_pattern = r"https?://(?!localhost|127\.0\.0\.1)[^\s\"']+"
        for m in re.finditer(url_pattern, schema_str, re.IGNORECASE):
            findings.append(
                RiskFinding(
                    category=OWASPCategory.LLM03,
                    severity=RiskSeverity.MEDIUM,
                    rule_id="LLM03-001",
                    message="Tool schema has external URL — possible supply-chain risk",
                    evidence=m.group(0)[:80],
                    tool_name=tool.get("name", "unknown"),
                )
            )
            break
        return findings


class LLM05OutputHandlingRule(BaseRiskRule):
    def check(self, tool: dict[str, Any]) -> list[RiskFinding]:
        findings: list[RiskFinding] = []
        annotations = tool.get("annotations", {}) or {}
        if annotations.get("destructiveHint"):
            findings.append(
                RiskFinding(
                    category=OWASPCategory.LLM05,
                    severity=RiskSeverity.HIGH,
                    rule_id="LLM05-001",
                    message="Tool declares destructiveHint=true — may modify persistent state",
                    evidence="annotations.destructiveHint=true",
                    tool_name=tool.get("name", "unknown"),
                )
            )
        desc = tool.get("description", "").lower()
        exec_pattern = r"\b(execut|run\s+command|shell|subprocess|eval)\b"
        m = re.search(exec_pattern, desc, re.IGNORECASE)
        if m:
            findings.append(
                RiskFinding(
                    category=OWASPCategory.LLM05,
                    severity=RiskSeverity.MEDIUM,
                    rule_id="LLM05-002",
                    message="Tool mentions code execution — output may not be sanitised",
                    evidence=m.group(0),
                    tool_name=tool.get("name", "unknown"),
                )
            )
        return findings


class LLM06ExcessiveAgencyRule(BaseRiskRule):
    def check(self, tool: dict[str, Any]) -> list[RiskFinding]:
        findings: list[RiskFinding] = []
        text = tool.get("description", "").lower()
        matches: list[str] = []
        for kw in _EXCESSIVE_AGENCY_KEYWORDS:
            if re.search(kw, text, re.IGNORECASE):
                matches.append(re.sub(r"\\s\+", " ", kw))
        if len(matches) >= 2:
            findings.append(
                RiskFinding(
                    category=OWASPCategory.LLM06,
                    severity=RiskSeverity.HIGH,
                    rule_id="LLM06-001",
                    message=f"Tool description mentions {len(matches)} excessive-agency keywords",
                    evidence=", ".join(matches[:4]),
                    tool_name=tool.get("name", "unknown"),
                )
            )
        elif len(matches) == 1:
            findings.append(
                RiskFinding(
                    category=OWASPCategory.LLM06,
                    severity=RiskSeverity.MEDIUM,
                    rule_id="LLM06-002",
                    message="Tool description mentions one excessive-agency keyword",
                    evidence=matches[0],
                    tool_name=tool.get("name", "unknown"),
                )
            )
        return findings


ALL_RULES: list[BaseRiskRule] = [
    LLM01PromptInjectionRule(),
    LLM03SupplyChainRule(),
    LLM05OutputHandlingRule(),
    LLM06ExcessiveAgencyRule(),
]
