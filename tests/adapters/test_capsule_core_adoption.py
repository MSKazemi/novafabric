"""The ``adapters/_capsule.py`` docstring names the adapters that keep their own capsule body.

Regression: it said "the eleven adapters that predate this module each carry
their own copy" when eight did — eleven is the total number of framework
adapters, three of which already use the shared core. Derive both lists from
the code and hold the docstring to them.
"""

from __future__ import annotations

import re
from pathlib import Path

import novafabric.adapters._capsule as capsule_core

ADAPTERS = Path(capsule_core.__file__).resolve().parent

_NUMBER_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
    7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve",
}


def _public_adapter_sources() -> dict[str, str]:
    return {
        p.stem: p.read_text(encoding="utf-8")
        for p in sorted(ADAPTERS.glob("*.py"))
        if not p.stem.startswith("_")
    }


def _uses_shared_core(src: str) -> bool:
    return "novafabric.adapters._capsule" in src


def _own_body_adapters() -> set[str]:
    """Adapters that open the capsule and claim wire hooks themselves."""
    return {
        name
        for name, src in _public_adapter_sources().items()
        if "wire_capture_state" in src and not _uses_shared_core(src)
    }


def _shared_core_adapters() -> set[str]:
    return {name for name, src in _public_adapter_sources().items() if _uses_shared_core(src)}


def _docstring() -> str:
    doc = capsule_core.__doc__
    assert doc
    return " ".join(doc.split())


def test_docstring_lists_exactly_the_adapters_with_their_own_body() -> None:
    m = re.search(r"each carry their own copy of that body: (.+?)\.", _docstring())
    assert m, "docstring no longer lists the adapters with their own capsule body"
    listed = set(re.findall(r"``(\w+)``", m.group(1)))
    assert listed == _own_body_adapters()


def test_docstring_count_matches_the_code() -> None:
    own = _own_body_adapters()
    word = _NUMBER_WORDS[len(own)]
    doc = _docstring()
    assert f"The {word} adapters that predate this module" in doc
    assert f"rewriting {word} working, tested adapters" in doc


def test_docstring_names_the_adapters_on_the_shared_core() -> None:
    m = re.search(r"New adapters use this instead \((.+?) do\)", _docstring())
    assert m
    assert set(re.findall(r"``(\w+)``", m.group(1))) == _shared_core_adapters()


def test_counts_are_the_ones_the_review_found() -> None:
    """Seven with their own body plus four on the core = the eleven framework adapters
    (bedrock_agentcore moved onto the core for review eval-adapters-query-12)."""
    assert len(_own_body_adapters()) == 7
    assert len(_shared_core_adapters()) == 4
