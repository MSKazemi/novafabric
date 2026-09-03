"""The facet table in `docs/concepts.md` must not deny a CLI that exists.

That table carries a blanket claim about which evidence modules are Python-API
only. It was accurate when written and **became false when `nova a2a-card` and
`nova a2a-objects` shipped** — the package was still listed as command-less.

The mistake that produced it is worth naming, because a docs sweep does not catch
it: I checked *"is my new command documented?"* (it was, in the CLI reference) but
not *"does my new command falsify a statement somewhere else?"*. Those are
different questions, and only the second produces false documentation.

This guard closes that specific hole: if a package named in the table has a CLI
module, the table may not claim it has none.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONCEPTS = ROOT / "docs" / "concepts.md"
CLI_DIR = ROOT / "src" / "novafabric" / "cli"

#: The blanket denials this guard refuses to let go stale.
DENIALS = (
    "none of them registers a `nova` CLI command",
    "none of them register a `nova` CLI command",
)


def _tabled_packages() -> set[str]:
    """Package names appearing in the concepts.md facet table."""
    text = CONCEPTS.read_text(encoding="utf-8")
    # [a-z0-9_] — NOT [a-z_]. The first version of this regex could not match
    # `novafabric.a2a` because of the digit, so it silently dropped the one row
    # this guard exists for and passed while blind. Caught by red-green only.
    return set(re.findall(r"\|\s*`novafabric\.([a-z0-9_]+)`\s*\|", text))


def _packages_with_a_cli() -> set[str]:
    """Packages that have at least one `cli/<pkg>*.py` module registered."""
    main = (CLI_DIR / "main.py").read_text(encoding="utf-8")
    found = set()
    for path in CLI_DIR.glob("*.py"):
        stem = path.stem
        if stem in {"main", "__init__"}:
            continue
        # Only count it if main.py actually wires it up.
        if f"cli.{stem} import" not in main:
            continue
        found.add(stem.split("_")[0])
    return found


def test_the_table_finds_packages() -> None:
    """Non-vacuity: a broken regex would make every check below pass."""
    assert "a2a" in _tabled_packages(), (
        "the a2a row is missing from the parsed table — the package name "
        "contains a digit, and a [a-z_]+ character class silently drops it"
    )
    assert len(_tabled_packages()) >= 5, (
        f"only {len(_tabled_packages())} packages parsed from the concepts.md "
        "facet table — the regex is broken and this guard proves nothing"
    )


def test_the_table_does_not_deny_a_cli_that_exists() -> None:
    text = CONCEPTS.read_text(encoding="utf-8")
    denial_present = any(d in text for d in DENIALS)
    if not denial_present:
        return  # the blanket claim was removed or reworded; nothing to contradict

    contradicted = sorted(_tabled_packages() & _packages_with_a_cli())
    assert not contradicted, (
        "docs/concepts.md still claims none of the facet modules registers a "
        f"`nova` command, but these do: {contradicted}. Reword the claim rather "
        "than removing the command — a docs sweep that only asks 'is my new "
        "thing documented?' misses statements the new thing falsifies."
    )
