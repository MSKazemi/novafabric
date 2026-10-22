#!/usr/bin/env python3
# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Post-process the sharded CI unit run (ADR-0316). Stdlib only.

ci_test_report.py check-manifests --expected all.txt shard-*.txt
    Fail unless the shard manifests partition the full collection: no test
    missing (it would never run), none run twice, none unexpected.

ci_test_report.py durations --out tests/.test-durations.json junit-*.xml
    Per-file seconds from JUnit XML (``-o junit_family=xunit1``, which records
    each test's file). Merges into an existing file: files present in the XML
    are replaced, files absent from it keep their value, files deleted from
    the tree are dropped.

ci_test_report.py summary junit-*.xml
    Markdown for ``$GITHUB_STEP_SUMMARY``: totals, failures, slowest files and
    per-shard wall time, so a red or slow run is readable without the logs.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: How many failures / slow files the summary lists before truncating.
SUMMARY_LIMIT = 25


@dataclass
class JUnitTotals:
    """What a set of JUnit reports says, aggregated."""

    tests: int = 0
    failures: list[str] = field(default_factory=list)
    skipped: int = 0
    seconds_by_file: Counter[str] = field(default_factory=Counter)
    seconds_by_report: dict[str, float] = field(default_factory=dict)


def read_junit(paths: list[Path]) -> JUnitTotals:
    totals = JUnitTotals()
    for path in paths:
        root = ET.parse(path).getroot()  # noqa: S314 - our own CI artefact
        report_seconds = 0.0
        for case in root.iter("testcase"):
            totals.tests += 1
            seconds = float(case.get("time") or 0.0)
            report_seconds += seconds
            name = case.get("file")
            if name:
                totals.seconds_by_file[name] += seconds
            if case.find("failure") is not None or case.find("error") is not None:
                totals.failures.append(f"{case.get('classname', '')}::{case.get('name', '')}")
            elif case.find("skipped") is not None:
                totals.skipped += 1
        totals.seconds_by_report[path.stem] = report_seconds
    return totals


def check_manifests(expected: Path, shards: list[Path]) -> list[str]:
    """Return the problems with *shards* as a partition of *expected* (empty = OK)."""
    want = expected.read_text(encoding="utf-8").splitlines()
    if not want:
        return [f"{expected}: empty — the reference collection selected nothing"]
    seen: Counter[str] = Counter()
    for shard in shards:
        seen.update(shard.read_text(encoding="utf-8").splitlines())
    problems = []
    missing = sorted(set(want) - set(seen))
    dupes = sorted(n for n, c in seen.items() if c > 1)
    extra = sorted(set(seen) - set(want))
    for label, ids in (
        ("never ran (in no shard)", missing),
        ("ran in more than one shard", dupes),
        ("ran but not in the reference collection", extra),
    ):
        if ids:
            shown = "\n    ".join(ids[:SUMMARY_LIMIT])
            more = (
                f"\n    … and {len(ids) - SUMMARY_LIMIT} more" if len(ids) > SUMMARY_LIMIT else ""
            )
            problems.append(f"{len(ids)} test(s) {label}:\n    {shown}{more}")
    return problems


def merge_durations(existing: Path, seconds_by_file: Counter[str]) -> dict[str, float]:
    merged: dict[str, float] = {}
    if existing.exists():
        merged.update(json.loads(existing.read_text(encoding="utf-8")).get("files", {}))
    merged.update({name: round(s, 2) for name, s in seconds_by_file.items()})
    return {name: merged[name] for name in sorted(merged) if (REPO / name).is_file()}


def summary_markdown(totals: JUnitTotals) -> str:
    status = "❌ failed" if totals.failures else "✅ passed"
    lines = [
        f"### Unit suite: {status}",
        "",
        f"{totals.tests} tests · {len(totals.failures)} failed · {totals.skipped} skipped",
        "",
    ]
    if totals.failures:
        lines += ["#### Failures", ""]
        lines += [f"- `{name}`" for name in totals.failures[:SUMMARY_LIMIT]]
        if len(totals.failures) > SUMMARY_LIMIT:
            lines.append(f"- … and {len(totals.failures) - SUMMARY_LIMIT} more")
        lines.append("")
    if totals.seconds_by_report:
        lines += [
            "#### Test seconds per shard (sum over workers)",
            "",
            "| shard | seconds |",
            "|---|---:|",
        ]
        lines += [f"| {name} | {s:.0f} |" for name, s in sorted(totals.seconds_by_report.items())]
        lines.append("")
    if totals.seconds_by_file:
        lines += ["#### Slowest test files", "", "| file | seconds |", "|---|---:|"]
        lines += [
            f"| `{name}` | {s:.1f} |"
            for name, s in totals.seconds_by_file.most_common(SUMMARY_LIMIT)
        ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check-manifests", help="prove the shards partition the suite")
    check.add_argument("--expected", type=Path, required=True)
    check.add_argument("shards", type=Path, nargs="+")

    dur = sub.add_parser("durations", help="refresh per-file durations from JUnit XML")
    dur.add_argument("--out", type=Path, required=True)
    dur.add_argument("reports", type=Path, nargs="+")

    summ = sub.add_parser("summary", help="markdown summary of JUnit XML")
    summ.add_argument("reports", type=Path, nargs="+")

    args = parser.parse_args(argv)

    if args.command == "check-manifests":
        problems = check_manifests(args.expected, args.shards)
        for problem in problems:
            print(f"shard check: {problem}", file=sys.stderr)
        if problems:
            return 1
        print(f"shard check: OK — {len(args.shards)} shard(s) partition the suite")
        return 0

    totals = read_junit(args.reports)
    if args.command == "durations":
        if not totals.seconds_by_file:
            print(
                "durations: the reports record no test files — were they written "
                "with `-o junit_family=xunit1`?",
                file=sys.stderr,
            )
            return 1
        files = merge_durations(args.out, totals.seconds_by_file)
        args.out.write_text(
            json.dumps({"version": 1, "files": files}, indent=1) + "\n", encoding="utf-8"
        )
        print(f"durations: {len(files)} files, {sum(files.values()):.0f} s total -> {args.out}")
        return 0

    sys.stdout.write(summary_markdown(totals))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
