from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from novafabric.mcp_scanner.models import RiskFinding, ScanReport, ToolRiskSummary
from novafabric.mcp_scanner.risk_rules import ALL_RULES, BaseRiskRule


class ManifestError(ValueError):
    """The document is not an MCP manifest this scanner can read: nothing was scanned."""


def extract_tools(manifest: Any) -> tuple[str, str, list[dict[str, Any]]]:
    """``(server_name, version, tools)`` from a manifest or a ``tools/list`` response.

    Accepts a top-level ``tools`` list, or the MCP ``tools/list`` JSON-RPC response
    (``{"result": {"tools": [...]}}``). Anything else — not an object, no ``tools``
    list, a tool that is not an object — raises :class:`ManifestError`: a document
    the scanner did not read must never report "0 findings" (review cli-b-08).
    """
    if not isinstance(manifest, dict):
        raise ManifestError(
            "an MCP manifest is a JSON/YAML object with a `tools` list, not a "
            f"{type(manifest).__name__}: nothing was scanned"
        )
    holder = manifest
    if "tools" not in manifest and isinstance(manifest.get("result"), dict):
        holder = manifest["result"]  # a tools/list response
    tools = holder.get("tools")
    if not isinstance(tools, list):
        raise ManifestError(
            "no `tools` list found (expected a top-level `tools` array, or a tools/list "
            "response with `result.tools`): nothing was scanned"
        )
    for i, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise ManifestError(f"`tools[{i}]` is not an object: nothing was scanned")
    return str(manifest.get("name", "unknown")), str(manifest.get("version", "unknown")), tools


class RiskScanner:
    def __init__(self, rules: list[BaseRiskRule] | None = None) -> None:
        self._rules = rules if rules is not None else ALL_RULES

    def scan(self, manifest: dict[str, Any]) -> ScanReport:
        """Scan *manifest*; raises :class:`ManifestError` when it has no tools list."""
        server_name, version, raw_tools = extract_tools(manifest)
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
                "manifest_version": version,
                "tool_count": len(raw_tools),
                "rules_applied": len(self._rules),
            },
        )

    def scan_file(self, path: Path) -> ScanReport:
        """Scan a JSON/YAML manifest file; an unparseable one is a :class:`ManifestError`."""
        text = path.read_text()
        try:
            if path.suffix in {".yaml", ".yml"}:
                manifest = yaml.safe_load(text)
            else:
                manifest = json.loads(text)
        except (json.JSONDecodeError, yaml.YAMLError) as exc:
            raise ManifestError(f"{path.name} is not valid JSON/YAML: {exc}") from None
        return self.scan(manifest)
