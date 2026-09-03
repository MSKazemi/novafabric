#!/usr/bin/env python3
"""Generate docs/slo.md from tests/bench/slo_catalog.toml (ADR-0248).

Usage:
    python scripts/gen_slo_docs.py           # (re)write docs/slo.md
    python scripts/gen_slo_docs.py --check   # exit 1 if docs/slo.md is stale

Deterministic: same catalog in, byte-identical page out. Edit the catalog,
never the page — `tests/bench/test_slo_catalog.py` fails on drift.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CATALOG = REPO / "tests" / "bench" / "slo_catalog.toml"
OUT = REPO / "docs" / "slo.md"

_STATUS_ORDER = {"gated": 0, "measured": 1, "target": 2}
_STATUS_BADGE = {
    "gated": "**gated** — enforced in CI",
    "measured": "**measured** — observed on a stated date/hardware; expires",
    "target": "**target** — aspiration, not a promise",
}


def _fmt_value(entry: dict) -> str:
    value, unit = entry["value"], entry["unit"]
    if unit == "s":
        ms = value * 1000.0
        return f"{ms:g} ms" if ms < 1000 else f"{value:g} s"
    if unit == "x":
        return f"{value:g}×"
    return f"{value:g} {unit}"


def render() -> str:
    doc = tomllib.loads(CATALOG.read_text())
    entries = sorted(
        doc.get("entry", []), key=lambda e: (_STATUS_ORDER[e["status"]], e["id"])
    )
    lines = [
        "# Performance SLO catalog",
        "",
        "> **Generated file — do not edit.** Source:",
        "> [`tests/bench/slo_catalog.toml`](../tests/bench/slo_catalog.toml), rendered by",
        "> `scripts/gen_slo_docs.py`. A drift test fails when this page is stale.",
        "",
        "Every published NovaFabric performance number lives here, and every number",
        "carries exactly one honesty status:",
        "",
    ]
    lines += [f"- {_STATUS_BADGE[s]}" for s in ("gated", "measured", "target")]
    lines += [
        "",
        "A `gated` number cannot drift from its gate: the gate test reads its",
        "threshold from the same catalog that generates this page. A `measured`",
        "number older than its `revalidate_by` date fails the suite until re-measured",
        "or demoted — stale claims age out visibly. An absent entry means **no",
        "claim**, which is itself information.",
        "",
        "| Metric | Value | Status | Workload | Enforcement / provenance |",
        "|---|---|---|---|---|",
    ]
    for e in entries:
        if e["status"] == "gated":
            prov = f"`{e['gate']}` ({e['tier']})"
        elif e["status"] == "measured":
            prov = (
                f"measured {e['measured_on']} on {e['hardware']}; "
                f"revalidate by {e['revalidate_by']} — {e['source']}"
            )
        else:
            prov = e.get("source", "—")
        lines.append(
            f"| `{e['id']}` — {e['title']} | {_fmt_value(e)} | {e['status']} "
            f"| {e['workload']} | {prov} |"
        )
    lines += [
        "",
        "Workload shapes are named, not adjectival: a number is only comparable to",
        "the workload it states. Sizing guidance derived from these entries is",
        "planned (ADR-0248 D4) and will inherit the weakest label in its input chain.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    content = render()
    if "--check" in sys.argv[1:]:
        if not OUT.exists() or OUT.read_text() != content:
            print(f"{OUT} is stale — run: python scripts/gen_slo_docs.py")
            return 1
        print(f"{OUT} is current")
        return 0
    OUT.write_text(content)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
