"""`docs/integrations/README.md` is the front door — it must list every guide.

The directory once held a single page with no index, so a reader looking for
"how do I run this alongside X" had nowhere to land (issue #72). An index that
misses a guide added later recreates that problem one page at a time, so the
list is derived from the directory rather than maintained by hand.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INTEGRATIONS = REPO_ROOT / "docs" / "integrations"
INDEX = INTEGRATIONS / "README.md"

_LINK = re.compile(r"\]\(([^)#\s]+)")


def _linked(markdown: Path) -> set[str]:
    return set(_LINK.findall(markdown.read_text(encoding="utf-8")))


def test_every_integration_guide_is_listed_in_the_index() -> None:
    guides = {p.name for p in INTEGRATIONS.glob("*.md") if p.name != "README.md"}
    assert guides, "no integration guides found — the glob is wrong"
    missing = guides - _linked(INDEX)
    assert not missing, f"docs/integrations/README.md does not link: {sorted(missing)}"


def test_every_guide_in_the_index_carries_a_status_label() -> None:
    """Docs honesty rule: each row says works today / experimental / planned / future design."""
    labels = ("works today", "experimental", "planned", "future design")
    rows = [
        line for line in INDEX.read_text(encoding="utf-8").splitlines()
        if line.startswith("| [") and ".md)" in line
    ]
    assert rows, "the index has no guide rows"
    for row in rows:
        assert any(label in row.lower() for label in labels), row


def test_the_docs_map_links_the_integrations_index() -> None:
    assert "integrations/README.md" in _linked(REPO_ROOT / "docs" / "README.md")
