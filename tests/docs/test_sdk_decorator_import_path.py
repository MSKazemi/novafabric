"""Docs must name the in-process decorator by a path that imports.

Found 2026-10-09: nine doc pages and a replay error message wrote
``@novafabric.agent``, but the package exports no ``agent`` attribute — the
only import path is ``from novafabric.sdk.agent import agent``. A reader who
copied the spelling got ``AttributeError``. This guard fails if the spelling
returns while the top-level export still does not exist.
"""

from __future__ import annotations

import re
from pathlib import Path

import novafabric

REPO = Path(__file__).resolve().parents[2]
# ``novafabric.agent`` not followed by an identifier character, so
# ``novafabric.agents`` and ``novafabric.agent_x`` do not match.
_SPELLING = re.compile(r"@?novafabric\.agent(?![\w])")
# Span names such as ``novafabric.agent.<name>`` are OTel attribute strings,
# not import paths.
_SPAN_NAME = re.compile(r"novafabric\.agent\.")


def _surfaces() -> list[Path]:
    paths = [REPO / "README.md"]
    paths += sorted((REPO / "docs").rglob("*.md"))
    paths += sorted((REPO / "src" / "novafabric").rglob("*.py"))
    return [
        p
        for p in paths
        if "releases" not in p.parts and p.name != "CHANGELOG.md"
    ]


def test_the_decorator_is_importable_where_the_docs_say() -> None:
    from novafabric.sdk.agent import agent

    assert callable(agent)


def test_no_surface_names_a_top_level_novafabric_agent() -> None:
    if hasattr(novafabric, "agent"):
        return  # the spelling became valid; nothing to guard
    offenders = []
    for path in _surfaces():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _SPELLING.search(_SPAN_NAME.sub("", line)):
                offenders.append(f"{path.relative_to(REPO)}:{lineno}")
    assert not offenders, (
        "`novafabric.agent` does not exist; write `@agent` "
        "(`from novafabric.sdk.agent import agent`) instead:\n" + "\n".join(offenders)
    )
