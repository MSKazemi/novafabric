"""``robots.txt`` is a published file, and it no longer ships in the wheel.

``src/novafabric/serve/static/`` is the built dashboard bundle, mounted at ``/``
by ``nova serve``. Everything in it is packaged into the wheel and installed by
``pip install novafabric`` — which makes it a public surface even though it
looks like a build artifact.

That is not hypothetical. Until 2026-08-28 the published v0.101.0 wheel carried
``novafabric/serve/static/robots.txt`` containing a note addressed to the
maintainer by name ("RECOMMENDATION FOR MOHSEN: ..."), written while editing the
website and copied into the package by the build. Nothing referenced the file
from Python, nothing tested it, and it was served at ``/robots.txt`` on
localhost where a robots.txt has no effect at all.

Since ADR-0299 (issue novafabric-private#20, PR B) the bundle is dashboard-only
and ``robots.txt`` is not copied into it at all: on localhost it does nothing,
and ``https://novafabric.ai`` is the single robots authority. Two properties:

1. the wheel does not ship a ``robots.txt`` (the bug class cannot recur);
2. the source under ``web/public/`` does not address a named individual —
   internal voice must not reach a published artifact.
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


def test_robots_txt_is_not_shipped_in_the_wheel() -> None:
    """The dashboard bundle has no use for it; the public site owns robots.txt."""
    assert not SHIPPED.exists(), (
        f"{SHIPPED.relative_to(ROOT)} is back. serve/static is the dashboard only "
        "(ADR-0299); web/scripts/copy-dashboard.mjs must not copy robots.txt"
    )


def test_robots_txt_source_carries_no_personal_note() -> None:
    """A published robots.txt must not address one person by name."""
    if not SOURCE.is_file():  # pragma: no cover
        pytest.skip("not a source checkout (installed distribution)")
    lowered = SOURCE.read_text(encoding="utf-8").lower()
    found = [w for w in FORBIDDEN if w in lowered]
    assert not found, (
        f"{SOURCE.relative_to(ROOT)} contains {found}. Rewrite it as guidance "
        "addressed to the reader, not to the maintainer."
    )


def test_the_guard_is_looking_at_a_real_tree() -> None:
    """Anti-vacuity: an absent bundle would make the not-shipped check pass trivially."""
    assert (SHIPPED.parent / "dashboard" / "index.html").is_file(), (
        "serve/static has no dashboard shell — the not-shipped check is inert"
    )
