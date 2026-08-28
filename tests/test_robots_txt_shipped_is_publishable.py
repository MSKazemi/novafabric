"""``robots.txt`` ships to every user, so it must read like a published file.

``src/novafabric/serve/static/`` is a built copy of the marketing site under
``web/``, mounted at ``/`` by ``nova serve``. Everything in it is packaged into
the wheel and installed by ``pip install novafabric`` — which makes it a public
surface even though it looks like a build artifact.

That is not hypothetical. Until 2026-08-28 the published v0.101.0 wheel carried
``novafabric/serve/static/robots.txt`` containing a note addressed to the
maintainer by name ("RECOMMENDATION FOR MOHSEN: ..."), written while editing the
website and copied into the package by the build. Nothing referenced the file
from Python, nothing tested it, and it was served at ``/robots.txt`` on
localhost where a robots.txt has no effect at all.

Two properties are asserted here:

1. the shipped copy has not drifted from its source under ``web/public/``;
2. neither copy addresses a named individual — internal voice must not reach a
   published artifact.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "web" / "public" / "robots.txt"
SHIPPED = ROOT / "src" / "novafabric" / "serve" / "static" / "robots.txt"

# The maintainer's name is legitimate in README attribution and CITATION.cff.
# In a robots.txt it is never anything but a leaked note.
FORBIDDEN = ("mohsen",)


def _existing() -> list[Path]:
    return [p for p in (SOURCE, SHIPPED) if p.is_file()]


def test_shipped_robots_txt_matches_its_source() -> None:
    """The packaged copy must be a faithful copy, not an independent edit."""
    if not (SOURCE.is_file() and SHIPPED.is_file()):  # pragma: no cover
        pytest.skip("not a source checkout (installed distribution)")
    assert SHIPPED.read_text(encoding="utf-8") == SOURCE.read_text(
        encoding="utf-8"
    ), (
        f"{SHIPPED.relative_to(ROOT)} has drifted from "
        f"{SOURCE.relative_to(ROOT)}; the shipped copy is generated from it, so "
        "an edit to one alone is silently lost on the next build"
    )


@pytest.mark.parametrize("name", ["source", "shipped"])
def test_robots_txt_carries_no_personal_note(name: str) -> None:
    """A file that ships in the wheel must not address one person by name."""
    path = SOURCE if name == "source" else SHIPPED
    if not path.is_file():  # pragma: no cover
        pytest.skip(f"{path} absent")
    lowered = path.read_text(encoding="utf-8").lower()
    found = [w for w in FORBIDDEN if w in lowered]
    assert not found, (
        f"{path.relative_to(ROOT)} contains {found}, which ships to every user "
        "of `pip install novafabric`. Rewrite it as guidance addressed to the "
        "reader, not to the maintainer."
    )


def test_the_guard_is_looking_at_a_real_file() -> None:
    """Anti-vacuity: a skip-everything version of this test proves nothing."""
    assert _existing(), "neither robots.txt was found — the guard is inert"
