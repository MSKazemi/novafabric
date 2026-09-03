#!/usr/bin/env python3
"""npm-audit severity + waiver gate (ADR-0186, extended to the npm trees).

The Python dependency closure has had a severity + waiver gate since
ADR-0186 (``pip-audit.yml`` → ``pip_audit_gate.py``). The three publicly
tracked npm lockfiles (``web/``, ``packages/nova-dashboard/``,
``packages/nova-sdk-ts/``) had **no equivalent** — found 2026-09-04 with
eight HIGH advisories sitting in ``web/`` where no CI job would ever read
them. This gate closes that asymmetry with the same contract:

- **HIGH / CRITICAL advisories block.** npm audit embeds the severity in
  the report itself, so unlike the pip gate no network lookup is needed.
  An advisory whose severity is missing or unrecognised blocks too —
  fail closed; waive it explicitly if it is truly acceptable.
- **MODERATE / LOW / INFO advisories report without blocking.**
- A waiver (id, justification, expiry) suppresses blocking for its GHSA id
  until expiry. An **expired waiver fails the gate by construction**; a
  malformed waiver file fails it too — never silently skipped.
- Advisories are **deduplicated by id**: npm attributes one advisory to
  every package it reaches through, so a single vulnerable transitive
  surfaces as N package entries. One advisory is one decision.

Stdlib only. Exit codes: 0 clean (or fully waived), 1 blocking findings or
expired waiver, 2 malformed input (waiver file or report).

Usage:
    npm audit --json > npm-audit-report.json   # exits non-zero on findings;
                                               # THIS gate decides what blocks
    python3 scripts/npm_audit_gate.py \
        --report npm-audit-report.json \
        --waivers .npm-audit-waivers.toml [--label web]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Reuse the pip gate's waiver parser verbatim: one schema, one strictness,
# two ecosystems. Its module lives beside this one and is imported by path
# so the gate keeps working from any CWD (CI runs it from the repo root).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pip_audit_gate import Waiver, WaiverError, load_waivers  # noqa: E402

BLOCKING_SEVERITIES = frozenset({"HIGH", "CRITICAL", "UNKNOWN"})
KNOWN_SEVERITIES = frozenset({"INFO", "LOW", "MODERATE", "HIGH", "CRITICAL"})
_GHSA_RE = re.compile(r"GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}")


class ReportError(Exception):
    """Missing or malformed npm-audit JSON report."""


@dataclass(frozen=True)
class Advisory:
    vuln_id: str
    title: str
    severity: str  # upper-case, or UNKNOWN
    packages: tuple[str, ...]  # every package npm attributes it to

    @property
    def label(self) -> str:
        return f"{self.vuln_id} ({self.title}) via {', '.join(self.packages)}"


def _advisory_id(via: dict[str, object]) -> str:
    """The advisory's stable id: the GHSA from its URL, else its source number."""
    url = via.get("url")
    if isinstance(url, str) and (match := _GHSA_RE.search(url)):
        return match.group(0)
    return f"npm-advisory-{via.get('source', 'unknown')}"


def load_advisories(path: Path) -> list[Advisory]:
    """Parse ``npm audit --json`` into deduplicated advisories.

    Only dict entries in ``via`` are advisories; string entries are npm's
    "vulnerable because of another package" pointers and carry no advisory
    of their own — counting them would turn one vulnerability into six.
    """
    if not path.is_file():
        raise ReportError(f"npm-audit report not found: {path}")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReportError(f"npm-audit report is not valid JSON: {exc}") from exc
    vulnerabilities = report.get("vulnerabilities")
    if not isinstance(vulnerabilities, dict):
        raise ReportError(
            "npm-audit report has no 'vulnerabilities' object"
            " -- was it produced by `npm audit --json`?"
        )

    titles: dict[str, str] = {}
    severities: dict[str, str] = {}
    packages: dict[str, set[str]] = {}
    for package, entry in vulnerabilities.items():
        for via in entry.get("via") or ():
            if not isinstance(via, dict):
                continue
            vuln_id = _advisory_id(via)
            severity = via.get("severity")
            if vuln_id not in titles:
                titles[vuln_id] = str(via.get("title") or "<untitled>")
                severities[vuln_id] = (
                    severity.upper()
                    if isinstance(severity, str) and severity.upper() in KNOWN_SEVERITIES
                    else "UNKNOWN"
                )
            packages.setdefault(vuln_id, set()).add(package)

    return [
        Advisory(
            vuln_id=vuln_id,
            title=titles[vuln_id],
            severity=severities[vuln_id],
            packages=tuple(sorted(packages[vuln_id])),
        )
        for vuln_id in sorted(titles)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True, help="npm audit --json output")
    parser.add_argument("--waivers", type=Path, required=True, help=".npm-audit-waivers.toml path")
    parser.add_argument("--label", default="", help="tree name for messages, e.g. 'web'")
    args = parser.parse_args(argv)
    tag = f"npm-audit-gate[{args.label}]" if args.label else "npm-audit-gate"

    try:
        waivers: list[Waiver] = load_waivers(args.waivers)
        advisories = load_advisories(args.report)
    except (WaiverError, ReportError) as exc:
        print(f"{tag}: ERROR: {exc}", file=sys.stderr)
        return 2

    # Expired waivers fail by construction — waivers cannot rot into permanence.
    today = dt.datetime.now(dt.timezone.utc).date()
    if expired := [w for w in waivers if w.expires < today]:
        for waiver in expired:
            print(
                f"{tag}: EXPIRED WAIVER: {waiver.vuln_id} expired"
                f" {waiver.expires.isoformat()} — remove it or renew it with"
                " a fresh justification and expiry",
                file=sys.stderr,
            )
        return 1

    waived_ids = {w.vuln_id: w for w in waivers}
    blocking: list[Advisory] = []
    for advisory in advisories:
        matched = waived_ids.get(advisory.vuln_id)
        if matched is not None:
            print(
                f"{tag}: waived: {advisory.label} — until {matched.expires.isoformat()}"
                f" ({matched.justification})"
            )
        elif advisory.severity in BLOCKING_SEVERITIES:
            blocking.append(advisory)
        else:
            print(f"{tag}: non-blocking ({advisory.severity}): {advisory.label}")

    if unused := set(waived_ids) - {a.vuln_id for a in advisories}:
        for vuln_id in sorted(unused):
            print(f"{tag}: note: waiver for {vuln_id} matched no advisory — consider removing it")

    if blocking:
        for advisory in blocking:
            hint = (
                " (severity missing from the report — failing closed)"
                if advisory.severity == "UNKNOWN"
                else ""
            )
            print(f"{tag}: BLOCKING ({advisory.severity}): {advisory.label}{hint}", file=sys.stderr)
        print(
            f"{tag}: FAIL — {len(blocking)} blocking advisory(ies)."
            " Fix by upgrading, or add a time-boxed entry to .npm-audit-waivers.toml"
            " (id, justification, expires).",
            file=sys.stderr,
        )
        return 1

    print(f"{tag}: OK — {len(advisories)} advisory(ies), none blocking.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
