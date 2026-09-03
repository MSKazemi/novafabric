#!/usr/bin/env python3
"""Generate the public architecture-decision index (``docs/decisions.md``).

NovaFabric records every architectural decision as a numbered ADR. The ADR
bodies are internal design records and are not published; the *index* is, so
that an ``ADR-0123`` reference anywhere in the public docs resolves to a real
title, status, and date rather than a dead link.

The generator reads the ADR frontmatter directly. It is a maintainer tool: the
source tree it reads is not present in a public clone, so the generated file is
committed and this script exits cleanly (rc 0) when the source is absent.

Usage::

    uv run python scripts/gen_decisions_index.py            # write docs/decisions.md
    uv run python scripts/gen_decisions_index.py --check     # fail if out of date
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ADR_DIR = REPO_ROOT / "design" / "adr"
OUTPUT = REPO_ROOT / "docs" / "decisions.md"

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_ADR_FILENAME = re.compile(r"\A(\d{4})-(.+)\.md\Z")

# Two ADR generations coexist: the newer ones carry YAML frontmatter, the older
# ones only an HTML comment header plus a bolded `**Status:**` line in the body.
# Read both rather than reporting the older half as "unknown".
_BODY_STATUS = re.compile(r"^(?:\*\*Status:\*\*|-\s+Status:)\s*(.+?)$", re.MULTILINE)
_BODY_DATE = re.compile(r"^\*\*Date:\*\*\s*(\d{4}-\d{2}-\d{2})", re.MULTILINE)
_BODY_TITLE = re.compile(r"^#\s+ADR-\d{4}\s*[—-]\s*(.+?)\s*$", re.MULTILINE)
_ISO_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")

KNOWN_STATUSES = ("superseded", "rejected", "withdrawn", "accepted", "proposed", "draft")

HEADER = """<!-- GENERATED FILE — do not edit by hand.
     Regenerate with: uv run python scripts/gen_decisions_index.py -->

# Architecture decisions

NovaFabric records every architectural decision as a numbered **ADR**
(Architecture Decision Record). This page is the canonical index: whenever the
documentation cites `ADR-0123`, this table is what it refers to.

**Why only an index?** ADR bodies are internal design records — they carry
in-progress research, competitive analysis, and commercial reasoning alongside
the technical decision, so the project publishes the decision *ledger* rather
than the deliberation. Everything an ADR decides that affects you as a user or
contributor is reflected in the code, the [CHANGELOG](../CHANGELOG.md), the
[ROADMAP](../ROADMAP.md), and the docs in this directory. If a decision here
matters to something you are building and the public docs do not explain it,
[open a Discussion](https://github.com/MSKazemi/novafabric/discussions) and
ask — we will write it up.

**Proposing a change to a decision** is the [RFC
process](governance/rfc-process.md), not an ADR. RFCs are public and live in
[`docs/rfcs/`](rfcs/).

| Status | Meaning |
|---|---|
| **accepted** | Decided and in force. |
| **proposed** | Written, not yet decided. |
| **superseded** | Replaced by a later ADR. |
| **rejected** | Considered and declined; kept as provenance. |

"""


@dataclass(frozen=True)
class Adr:
    number: str
    title: str
    status: str
    created_at: str

    @property
    def label(self) -> str:
        return f"ADR-{self.number}"


def _scalar(frontmatter: str, key: str) -> str:
    """Pull a single scalar value out of YAML frontmatter.

    Deliberately not a YAML parse: the frontmatter here is machine-written and
    flat, and this keeps the script dependency-free so it can run in any
    environment including a bare CI container.
    """
    match = re.search(rf"^{key}:\s*(.+?)\s*$", frontmatter, re.MULTILINE)
    if not match:
        return ""
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value.strip()


def _title_from_slug(slug: str) -> str:
    return slug.replace("-", " ").capitalize()


def _normalize_status(raw: str) -> str:
    """Reduce a free-form status line to one known token.

    ADR status lines are prose as often as they are keywords — e.g. ``**Accepted**
    2026-07-30 — raised by the enterprise-readiness audit``. The index needs the
    verdict, not the commentary. ``superseded`` and ``rejected`` are checked
    before ``accepted`` because a superseding line commonly restates the original
    acceptance.
    """
    lowered = raw.lower()
    for status in KNOWN_STATUSES:
        if status in lowered:
            return status
    return "unknown"


def collect(adr_dir: Path = ADR_DIR) -> list[Adr]:
    adrs: list[Adr] = []
    for path in sorted(adr_dir.glob("*.md")):
        name_match = _ADR_FILENAME.match(path.name)
        if not name_match:
            continue
        number, slug = name_match.groups()
        text = path.read_text(encoding="utf-8")
        fm_match = _FRONTMATTER.match(text)
        frontmatter = fm_match.group(1) if fm_match else ""

        body_status = _BODY_STATUS.search(text)
        raw_status = _scalar(frontmatter, "status") or (body_status.group(1) if body_status else "")
        status = _normalize_status(raw_status)

        body_title = _BODY_TITLE.search(text)
        raw_title = _scalar(frontmatter, "title") or (body_title.group(1) if body_title else "")
        title = raw_title or _title_from_slug(slug)

        created_at = _scalar(frontmatter, "created_at")
        if not created_at:
            body_date = _BODY_DATE.search(text)
            if body_date:
                created_at = body_date.group(1)
            elif body_status:
                # The status line usually carries the decision date inline.
                inline = _ISO_DATE.search(body_status.group(1))
                created_at = inline.group(1) if inline else ""

        adrs.append(
            Adr(
                number=number,
                title=title.strip().rstrip("."),
                status=status,
                created_at=created_at or "—",
            )
        )
    return adrs


def render(adrs: list[Adr]) -> str:
    by_status: dict[str, int] = {}
    for adr in adrs:
        by_status[adr.status] = by_status.get(adr.status, 0) + 1
    counts = " · ".join(f"**{count}** {status}" for status, count in sorted(by_status.items()))

    lines = [HEADER]
    lines.append(f"**{len(adrs)} decisions recorded** — {counts}.\n")
    lines.append("| ADR | Title | Status | Date |")
    lines.append("|---|---|---|---|")
    for adr in adrs:
        title = adr.title.replace("|", "\\|")
        lines.append(f"| `{adr.label}` | {title} | {adr.status} | {adr.created_at} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if docs/decisions.md is out of date instead of writing it",
    )
    args = parser.parse_args(argv)

    if not ADR_DIR.is_dir():
        # Public clone: the design tree is not present. The generated index is
        # committed, so there is nothing to do and nothing is wrong.
        print(f"{ADR_DIR} not present — skipping (generated index is committed)")
        return 0

    adrs = collect()
    if not adrs:
        print(f"no ADRs found in {ADR_DIR}", file=sys.stderr)
        return 1

    rendered = render(adrs)
    current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""

    if args.check:
        if current != rendered:
            print(
                f"{OUTPUT.relative_to(REPO_ROOT)} is out of date — "
                "run: uv run python scripts/gen_decisions_index.py",
                file=sys.stderr,
            )
            return 1
        print(f"{OUTPUT.relative_to(REPO_ROOT)} is up to date ({len(adrs)} decisions)")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)} ({len(adrs)} decisions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
