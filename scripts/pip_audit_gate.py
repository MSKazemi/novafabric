#!/usr/bin/env python3
"""pip-audit severity + waiver gate (ADR-0186).

Reads a pip-audit JSON report and the checked-in waiver file
(``.pip-audit-waivers.toml``), then decides what blocks CI:

- **HIGH / CRITICAL findings block.** Severity comes from the OSV.dev record
  (``database_specific.severity`` when present, else the CVSS v3 base score
  computed from the record's ``severity`` vector; checked across the
  finding's id and all aliases, GHSA ids first). A finding whose severity
  cannot be determined blocks too — fail closed; waive it explicitly if it
  is truly acceptable.
- **MODERATE / LOW findings report without blocking.**
- A waiver (id, justification, expiry) suppresses blocking for its id until
  expiry. An **expired waiver fails the gate by construction**; a malformed
  waiver file fails it too — never silently skipped.

Stdlib only. Exit codes: 0 clean (or fully waived), 1 blocking findings or
expired waiver, 2 malformed input (waiver file or report).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

OSV_API = "https://api.osv.dev/v1/vulns/{vuln_id}"
# Severities that block the gate. UNKNOWN is deliberate: fail closed when the
# advisory source does not state a severity or cannot be reached.
BLOCKING_SEVERITIES = frozenset({"HIGH", "CRITICAL", "UNKNOWN"})
REQUIRED_WAIVER_KEYS = frozenset({"id", "justification", "expires"})


class WaiverError(Exception):
    """Malformed waiver file — fails the gate, never silently skipped."""


class ReportError(Exception):
    """Missing or malformed pip-audit JSON report."""


@dataclass(frozen=True)
class Waiver:
    vuln_id: str
    justification: str
    expires: dt.date


@dataclass(frozen=True)
class Finding:
    package: str
    version: str
    vuln_id: str
    aliases: tuple[str, ...]
    fix_versions: tuple[str, ...]

    @property
    def all_ids(self) -> frozenset[str]:
        return frozenset({self.vuln_id, *self.aliases})

    @property
    def label(self) -> str:
        fixes = ", ".join(self.fix_versions) or "no fix released"
        return f"{self.vuln_id} in {self.package} {self.version} (fix: {fixes})"


def _parse_expiry(raw: object, where: str) -> dt.date:
    if isinstance(raw, dt.datetime):  # TOML offset/local date-time
        return raw.date()
    if isinstance(raw, dt.date):  # TOML bare date
        return raw
    if isinstance(raw, str):
        try:
            return dt.date.fromisoformat(raw)
        except ValueError as exc:
            msg = f"{where}: 'expires' is not an ISO date (YYYY-MM-DD): {raw!r}"
            raise WaiverError(msg) from exc
    raise WaiverError(f"{where}: 'expires' must be an ISO date, got {type(raw).__name__}")


def load_waivers(path: Path) -> list[Waiver]:
    """Parse and validate the waiver file. Any malformation raises WaiverError."""
    if not path.is_file():
        raise WaiverError(f"waiver file not found: {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise WaiverError(f"waiver file is not valid TOML: {exc}") from exc

    entries = data.pop("waiver", [])
    if data:
        raise WaiverError(f"unknown top-level keys in waiver file: {sorted(data)}")
    if not isinstance(entries, list):
        raise WaiverError("'waiver' must be an array of tables ([[waiver]])")

    waivers: list[Waiver] = []
    for index, entry in enumerate(entries):
        where = f"[[waiver]] entry #{index + 1}"
        if not isinstance(entry, dict):
            raise WaiverError(f"{where}: must be a table")
        keys = frozenset(entry)
        if missing := REQUIRED_WAIVER_KEYS - keys:
            raise WaiverError(f"{where}: missing required field(s): {sorted(missing)}")
        if extra := keys - REQUIRED_WAIVER_KEYS:
            raise WaiverError(f"{where}: unknown field(s): {sorted(extra)}")
        vuln_id, justification = entry["id"], entry["justification"]
        if not isinstance(vuln_id, str) or not vuln_id.strip():
            raise WaiverError(f"{where}: 'id' must be a non-empty string")
        if not isinstance(justification, str) or not justification.strip():
            raise WaiverError(f"{where}: 'justification' must be a non-empty string")
        waivers.append(
            Waiver(
                vuln_id=vuln_id.strip(),
                justification=justification.strip(),
                expires=_parse_expiry(entry["expires"], where),
            )
        )
    return waivers


def load_findings(path: Path) -> list[Finding]:
    """Parse the pip-audit ``--format json`` report into findings."""
    if not path.is_file():
        raise ReportError(f"pip-audit report not found: {path}")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReportError(f"pip-audit report is not valid JSON: {exc}") from exc
    dependencies = report.get("dependencies")
    if not isinstance(dependencies, list):
        raise ReportError("pip-audit report has no 'dependencies' array")

    findings: list[Finding] = []
    for dep in dependencies:
        for vuln in dep.get("vulns") or []:
            findings.append(
                Finding(
                    package=dep.get("name", "<unknown>"),
                    version=dep.get("version", "<unknown>"),
                    vuln_id=vuln["id"],
                    aliases=tuple(vuln.get("aliases") or ()),
                    fix_versions=tuple(vuln.get("fix_versions") or ()),
                )
            )
    return findings


# CVSS v3.x base-score metric weights (CVSS v3.1 specification §7.1–7.4).
_CVSS3_AV = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}
_CVSS3_AC = {"L": 0.77, "H": 0.44}
_CVSS3_PR_UNCHANGED = {"N": 0.85, "L": 0.62, "H": 0.27}
_CVSS3_PR_CHANGED = {"N": 0.85, "L": 0.68, "H": 0.5}
_CVSS3_UI = {"N": 0.85, "R": 0.62}
_CVSS3_CIA = {"H": 0.56, "L": 0.22, "N": 0.0}


def cvss3_base_score(vector: str) -> float | None:
    """Compute the CVSS v3.x base score from a vector string, or None."""
    parts = dict(p.split(":", 1) for p in vector.split("/") if ":" in p)
    if not parts.get("CVSS", "").startswith("3"):
        return None
    try:
        scope_changed = {"U": False, "C": True}[parts["S"]]
        pr_weights = _CVSS3_PR_CHANGED if scope_changed else _CVSS3_PR_UNCHANGED
        exploitability = (
            8.22
            * _CVSS3_AV[parts["AV"]]
            * _CVSS3_AC[parts["AC"]]
            * pr_weights[parts["PR"]]
            * _CVSS3_UI[parts["UI"]]
        )
        iss = 1 - (
            (1 - _CVSS3_CIA[parts["C"]])
            * (1 - _CVSS3_CIA[parts["I"]])
            * (1 - _CVSS3_CIA[parts["A"]])
        )
    except KeyError:
        return None
    impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if scope_changed else 6.42 * iss
    if impact <= 0:
        return 0.0
    raw = min((1.08 if scope_changed else 1.0) * (impact + exploitability), 10.0)
    return math.ceil(raw * 10) / 10  # spec Roundup(): ceil to one decimal


def _severity_from_score(score: float) -> str:
    if score >= 9.0:
        return "CRITICAL"
    if score >= 7.0:
        return "HIGH"
    if score >= 4.0:
        return "MODERATE"
    return "LOW" if score > 0 else "NONE"


def severity_from_record(record: dict[str, object]) -> str | None:
    """Extract a severity rating from one OSV record, or None."""
    database_specific = record.get("database_specific")
    if isinstance(database_specific, dict):
        severity = database_specific.get("severity")
        if isinstance(severity, str) and severity:
            return severity.upper()
    entries = record.get("severity")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict) and entry.get("type") == "CVSS_V3":
                score = cvss3_base_score(str(entry.get("score", "")))
                if score is not None:
                    return _severity_from_score(score)
    return None


def fetch_osv_record(
    vuln_id: str, timeout: float = 10.0, attempts: int = 3
) -> dict[str, object] | None:
    """Fetch one OSV record by id, or None (404 / persistent failure)."""
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(OSV_API.format(vuln_id=vuln_id), timeout=timeout) as resp:
                record = json.load(resp)
                return record if isinstance(record, dict) else None
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            continue  # 5xx / rate limit — retry
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            continue
    return None


def finding_severity(finding: Finding) -> str:
    """Best-known severity across the finding's ids; UNKNOWN fails closed."""
    # GHSA records carry a curated database_specific.severity — try them first.
    ordered = sorted(finding.all_ids, key=lambda i: (not i.startswith("GHSA-"), i))
    for vuln_id in ordered:
        record = fetch_osv_record(vuln_id)
        if record is not None and (severity := severity_from_record(record)):
            return severity
    return "UNKNOWN"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True, help="pip-audit --format json output")
    parser.add_argument("--waivers", type=Path, required=True, help=".pip-audit-waivers.toml path")
    args = parser.parse_args(argv)

    try:
        waivers = load_waivers(args.waivers)
        findings = load_findings(args.report)
    except (WaiverError, ReportError) as exc:
        print(f"pip-audit-gate: ERROR: {exc}", file=sys.stderr)
        return 2

    # Expired waivers fail by construction — waivers cannot rot into permanence.
    today = dt.datetime.now(dt.timezone.utc).date()
    if expired := [w for w in waivers if w.expires < today]:
        for waiver in expired:
            print(
                f"pip-audit-gate: EXPIRED WAIVER: {waiver.vuln_id} expired"
                f" {waiver.expires.isoformat()} — remove it or renew it with"
                " a fresh justification and expiry",
                file=sys.stderr,
            )
        return 1

    waived_ids = {w.vuln_id: w for w in waivers}
    blocking: list[tuple[str, Finding]] = []
    for finding in findings:
        matched = [waived_ids[i] for i in finding.all_ids if i in waived_ids]
        if matched:
            waiver = matched[0]
            print(
                f"pip-audit-gate: waived: {finding.label} — until {waiver.expires.isoformat()}"
                f" ({waiver.justification})"
            )
            continue
        severity = finding_severity(finding)
        if severity in BLOCKING_SEVERITIES:
            blocking.append((severity, finding))
        else:
            print(f"pip-audit-gate: non-blocking ({severity}): {finding.label}")

    if unused := set(waived_ids) - {i for f in findings for i in f.all_ids}:
        for vuln_id in sorted(unused):
            print(
                f"pip-audit-gate: note: waiver for {vuln_id} matched no finding"
                " — consider removing it"
            )

    if blocking:
        for severity, finding in blocking:
            hint = (
                " (severity undeterminable via OSV — failing closed)"
                if severity == "UNKNOWN"
                else ""
            )
            print(f"pip-audit-gate: BLOCKING ({severity}): {finding.label}{hint}", file=sys.stderr)
        print(
            f"pip-audit-gate: FAIL — {len(blocking)} blocking finding(s)."
            " Fix by upgrading, or add a time-boxed entry to .pip-audit-waivers.toml"
            " (id, justification, expires).",
            file=sys.stderr,
        )
        return 1

    print(f"pip-audit-gate: OK — {len(findings)} finding(s), none blocking.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
