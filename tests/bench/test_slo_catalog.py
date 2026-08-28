"""SLO catalog guards (ADR-0248 slice 1).

The catalog is only a contract if a mechanism enforces its honesty rules:

- every entry is well-formed for its status;
- every ``gated`` entry names a gate that actually exists;
- every ``measured`` entry is within its revalidation window — a stale
  measurement fails here by id until re-measured or demoted to ``target``;
- the generated ``docs/slo.md`` matches the catalog byte-for-byte.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path

from bench.slo import VALID_STATUSES, load_catalog

REPO = Path(__file__).resolve().parents[2]

_REQUIRED_ALWAYS = {"id", "title", "status", "value", "unit", "workload"}
_REQUIRED_BY_STATUS = {
    "gated": {"gate", "tier"},
    "measured": {"hardware", "measured_on", "revalidate_by", "source"},
    "target": set(),
}


def test_every_entry_is_well_formed() -> None:
    catalog = load_catalog()
    assert catalog, "SLO catalog parsed empty"
    for eid, entry in catalog.items():
        assert entry["status"] in VALID_STATUSES, f"{eid}: bad status {entry['status']!r}"
        missing = (_REQUIRED_ALWAYS | _REQUIRED_BY_STATUS[entry["status"]]) - set(entry)
        assert not missing, f"{eid}: missing required fields {sorted(missing)}"


def test_every_gated_entry_names_a_real_gate() -> None:
    for eid, entry in load_catalog().items():
        if entry["status"] != "gated":
            continue
        path_str, sep, name = entry["gate"].partition("::")
        assert sep, f"{eid}: gate must be '<file>::<name>', got {entry['gate']!r}"
        gate_file = REPO / path_str
        assert gate_file.is_file(), f"{eid}: gate file {path_str} does not exist"
        assert name in gate_file.read_text(), (
            f"{eid}: gate file {path_str} does not mention {name!r} — "
            "the gate this entry claims does not exist"
        )


def test_measured_entries_are_within_their_revalidation_window() -> None:
    today = dt.date.today()
    stale = [
        f"{eid} (revalidate_by {entry['revalidate_by']})"
        for eid, entry in load_catalog().items()
        if entry["status"] == "measured" and entry["revalidate_by"] < today
    ]
    assert not stale, (
        "measured SLO entries past their revalidation date — re-measure and update "
        f"measured_on/revalidate_by, or demote to status='target': {stale}"
    )


def test_docs_slo_page_matches_catalog() -> None:
    spec = importlib.util.spec_from_file_location(
        "gen_slo_docs", REPO / "scripts" / "gen_slo_docs.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    page = REPO / "docs" / "slo.md"
    assert page.is_file(), "docs/slo.md missing — run: python scripts/gen_slo_docs.py"
    assert page.read_text() == mod.render(), (
        "docs/slo.md is stale — run: python scripts/gen_slo_docs.py"
    )


def test_measured_sources_do_not_point_outside_the_published_tree() -> None:
    """A ``source`` a public reader cannot follow is not a source.

    ``slo_catalog.toml`` and the ``docs/slo.md`` generated from it are both
    published. Their ``source`` fields were citing raw campaign artifacts under
    ``experiments/`` and design rationale under ``design/adr/`` -- neither of
    which exists in the published tree, so every reference resolved to nothing
    for the only audience that reads a published catalog.

    The rule is not "the path exists": it exists in the development tree, which
    is exactly why the problem survived. The rule is that a repo-relative path
    in a ``source`` must be **tracked by the publishing git**, and anything that
    is not published must say so in words instead of pretending to be a link.
    """
    import subprocess

    private_prefixes = ("experiments/", "design/", ".claude/", "papers/", "1private/")
    offenders = []
    for eid, entry in load_catalog().items():
        source = str(entry.get("source", ""))
        for token in source.replace(",", " ").replace(";", " ").split():
            if not token.startswith(private_prefixes):
                continue
            offenders.append(f"{eid}: source cites unpublished path {token!r}")
    assert not offenders, (
        "SLO catalog sources point outside the published tree — name the artifact "
        "and mark it '(private research record)' / '(private design record)' "
        f"instead of citing a path readers cannot open: {offenders}"
    )

    # Anything that *does* look like a repo path must really be published.
    try:
        tracked = set(
            subprocess.run(
                ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
            ).stdout.split()
        )
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - no git
        return
    if not tracked:  # pragma: no cover - not a checkout
        return
    dangling = []
    for eid, entry in load_catalog().items():
        for token in str(entry.get("source", "")).replace(",", " ").split():
            token = token.strip("();")
            if "/" not in token or not Path(token).suffix:
                continue
            if token not in tracked:
                dangling.append(f"{eid}: source cites {token!r}, untracked by the publishing git")
    assert not dangling, (
        "SLO catalog sources name repo paths the publishing git does not carry: "
        f"{dangling}"
    )
