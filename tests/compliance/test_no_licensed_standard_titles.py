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

"""Licensed standards' clause and criterion headings do not ship as NovaFabric text.

ISO/IEC 42001 and the AICPA Trust Services Criteria (SOC 2) are copyrighted. The
repository cites their identifiers (``6.1``, ``CC6.6``) and describes each one in its own
words; users who need the official wording load a licensed catalog by path. See
``docs/developer-guide.md`` ("Standards text").

The guard stores truncated SHA-256 digests of the publishers' headings and phrases,
never the text, so it does not itself reproduce what it forbids. Two checks:

* every built-in control title of a copyrighted framework (the ``nova audit`` profiles
  and the ``nova export-examiner iso42001`` clause table) is tested window by window
  against the digests of the publishers' headings, short ones included;
* every text file under ``src/``, ``schemas/``, ``tests/fixtures/`` and ``docs/`` is
  scanned for the longer, distinctive phrases (four words or more). Short headings such
  as two-word terms of art are only forbidden as a title, not in prose.

Words are compared lower-cased with punctuation removed, so a change of case or a
dropped comma does not get a copied heading past the guard.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from pathlib import Path

from novafabric.compliance.audit.loader import list_profiles, load_profile
from novafabric.compliance.export.examiner import _ISO42001_CONTROLS

_REPO = Path(__file__).resolve().parents[2]

#: Frameworks whose criteria text is copyrighted by its publisher.
COPYRIGHTED_FRAMEWORKS: frozenset[str] = frozenset({"SOC 2 Type 2", "ISO/IEC 42001"})

#: (word count, digest) of ISO/IEC 42001 clause headings and AICPA TSC headings/phrases.
#: A built-in title may not contain any of them as a run of consecutive words.
TITLE_DIGESTS: frozenset[tuple[int, str]] = frozenset({
    (2, "0fba036c5f2cadb4"), (2, "132115fd42f558dc"), (2, "2177228b40fbe5f0"),
    (2, "5fb86ce4ae7e5d99"), (2, "6c42dd8107ae87e0"), (2, "7be96c1162c0c7bc"),
    (2, "983ddfed8f4d8f91"), (2, "aeb72f87d9e9bc1e"), (2, "b09332e6e46456eb"),
    (2, "bcf5f37ac55fedb7"), (2, "cd29a45b5c99f43d"), (2, "d1a6e40d30916d07"),
    (2, "f2f4aa255386098a"),
    (3, "3d7f35fafb371325"), (3, "43d2e3ee847fefff"), (3, "88b3d9497111791c"),
    (3, "9692a7fbca240ceb"), (3, "a62d5b5925995cfe"), (3, "d97580e08e673672"),
    (3, "eb9d26da3b861f15"),
    (4, "198e74fa497149c4"), (4, "2d6a530cc934f419"), (4, "35eb0283f79f986d"),
    (4, "526f73907e17cfeb"), (4, "aee3a1344f2876a7"), (4, "af0902813f44df64"),
    (4, "edf1dab9a58b6db9"),
    (5, "72b72057b50279d9"), (5, "f92bb72398e1a9d3"),
    (6, "08d339302447f7b1"), (6, "45582e35dcec5161"), (6, "78c6d917e91f9fa7"),
    (7, "f979ed60b0a74e64"),
    (8, "38a145b67f96102e"), (8, "5e3b2a8383448666"),
})

#: (word count, digest of the first word, digest of the phrase) of distinctive headings
#: and criterion phrases. The first-word digest only narrows which windows get hashed.
PHRASE_DIGESTS: frozenset[tuple[int, str, str]] = frozenset({
    (4, "66cf5513b37462be", "2d6a530cc934f419"),
    (4, "d5e4f7f12f0e5315", "35eb0283f79f986d"),
    (4, "7a256a9a7fc68c00", "526f73907e17cfeb"),
    (5, "14a2326b6bb54f40", "f92bb72398e1a9d3"),
    (6, "2b0dcdd40016096b", "08d339302447f7b1"),
    (6, "04214cad51df8166", "45582e35dcec5161"),
    (6, "04214cad51df8166", "78c6d917e91f9fa7"),
    (7, "f480ffb2979d1888", "54769c979c5c40c1"),
    (7, "ac7f39a1e0b7bcd0", "d4794d2342d0414b"),
    (7, "32e83e92d45d71f6", "f979ed60b0a74e64"),
    (8, "04214cad51df8166", "38a145b67f96102e"),
    (8, "7a87afa2c7184a50", "53d7dbfa73461071"),
    (8, "a00b69edd71c3618", "5e3b2a8383448666"),
    (9, "680dce633db1189e", "9e7a67328d9ef34c"),
    (10, "2c6ef0f0d0e4cc6e", "a646c12903391b2c"),
    (10, "6e302ff0f0e8ebf6", "ffc39c32f1d74e46"),
    (11, "e5c19e68e5a39343", "59a48d5160b974df"),
})

#: Directories scanned for distinctive phrases, and the text suffixes read in them.
SCANNED_DIRS: tuple[str, ...] = ("src", "schemas", "tests/fixtures", "docs")
SCANNED_SUFFIXES: frozenset[str] = frozenset({
    ".py", ".yaml", ".yml", ".json", ".md", ".txt", ".toml", ".rego", ".html",
})
#: Generated dashboard bundles (built from ui/, minified) are not hand-written text.
SKIPPED_PARTS: frozenset[str] = frozenset({"static", "node_modules", "__pycache__"})

_SUFFIX = re.compile(r"\s*\((?:§\s*)?[A-Z]*\d[\w.\-]*\)\s*$")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).split()


def _title_hits(title: str, digests: Iterable[tuple[int, str]] = TITLE_DIGESTS) -> set[str]:
    """Runs of *title* (minus a trailing ``(§6.1)``/``(CC6.6)``) matching a heading digest."""
    words = _words(_SUFFIX.sub("", title))
    wanted = frozenset(digests)
    hits: set[str] = set()
    for n in {n for n, _ in wanted}:
        for i in range(len(words) - n + 1):
            window = " ".join(words[i:i + n])
            if (n, _digest(window)) in wanted:
                hits.add(window)
    return hits


def _phrase_hits(
    text: str,
    digests: Iterable[tuple[int, str, str]] = PHRASE_DIGESTS,
    _word_digests: dict[str, str] | None = None,
) -> set[str]:
    """Runs of *text* matching a distinctive-phrase digest."""
    by_anchor: dict[str, set[tuple[int, str]]] = {}
    for n, anchor, full in digests:
        by_anchor.setdefault(anchor, set()).add((n, full))
    cache = _word_digests if _word_digests is not None else {}
    words = _words(text)
    hits: set[str] = set()
    for i, word in enumerate(words):
        anchor = cache.get(word)
        if anchor is None:
            anchor = cache[word] = _digest(word)
        for n, full in by_anchor.get(anchor, ()):
            window = " ".join(words[i:i + n])
            if len(window.split()) == n and _digest(window) == full:
                hits.add(window)
    return hits


def _builtin_titles() -> list[tuple[str, str, str]]:
    """(source, id, title) of every built-in title of a copyrighted framework."""
    rows: list[tuple[str, str, str]] = []
    for pid in list_profiles():
        prof = load_profile(pid)
        if prof.framework in COPYRIGHTED_FRAMEWORKS:
            rows.extend((pid, c.id, c.title) for c in prof.controls)
    rows.extend(("export-examiner iso42001", clause, ctrl["title"])
                for clause, ctrl in _ISO42001_CONTROLS.items())
    return rows


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for rel in SCANNED_DIRS:
        for path in sorted((_REPO / rel).rglob("*")):
            if (path.suffix in SCANNED_SUFFIXES and path.is_file()
                    and not SKIPPED_PARTS.intersection(path.relative_to(_REPO).parts)):
                files.append(path)
    return files


# --------------------------------------------------------------------- the guard


def test_the_title_guard_is_not_vacuous() -> None:
    frameworks = {load_profile(p).framework for p in list_profiles()}
    assert COPYRIGHTED_FRAMEWORKS <= frameworks
    sources = {src for src, _cid, _title in _builtin_titles()}
    assert {"soc2-type2", "iso42001", "export-examiner iso42001"} <= sources


def test_no_builtin_title_reuses_a_publisher_heading() -> None:
    offenders = [(src, cid) for src, cid, title in _builtin_titles() if _title_hits(title)]
    assert offenders == []


def test_examiner_titles_are_short_summaries_keyed_by_clause_number() -> None:
    for clause, ctrl in _ISO42001_CONTROLS.items():
        assert re.fullmatch(r"\d+(?:\.\d+)+", clause), clause
        assert 0 < len(ctrl["title"]) <= 60, clause


def test_no_distinctive_publisher_phrase_ships_in_text_files() -> None:
    files = _scanned_files()
    assert len(files) > 100  # the scan reached the tree
    cache: dict[str, str] = {}
    offenders = [
        str(path.relative_to(_REPO)) for path in files
        if _phrase_hits(path.read_text(encoding="utf-8", errors="replace"),
                        _word_digests=cache)
    ]
    assert offenders == []


def test_the_digest_tables_hold_digests_only() -> None:
    for n, digest in TITLE_DIGESTS:
        assert n >= 2 and re.fullmatch(r"[0-9a-f]{16}", digest)
    for n, anchor, digest in PHRASE_DIGESTS:
        assert n >= 4 and re.fullmatch(r"[0-9a-f]{16}", anchor)
        assert re.fullmatch(r"[0-9a-f]{16}", digest)


# ------------------------------------------------------------- the guard has teeth


def test_the_title_check_catches_a_planted_heading() -> None:
    planted = {(3, _digest("alpha beta gamma"))}
    assert _title_hits("Alpha, Beta -- gamma (§6.1)", planted) == {"alpha beta gamma"}
    assert _title_hits("Intro alpha beta gamma outro (CC6.6)", planted) == {"alpha beta gamma"}
    assert _title_hits("alpha gamma beta", planted) == set()


def test_the_phrase_scan_catches_a_planted_phrase() -> None:
    phrase = "alpha beta gamma delta"
    planted = {(4, _digest("alpha"), _digest(phrase))}
    assert _phrase_hits("Intro. Alpha, BETA gamma: delta! outro", planted) == {phrase}
    assert _phrase_hits("alpha beta gamma", planted) == set()  # too short, at the end
    assert _phrase_hits("alpha beta delta gamma", planted) == set()


def test_the_suffix_strip_keeps_the_words_of_the_title() -> None:
    assert _SUFFIX.sub("", "Improving over time (§10.1)") == "Improving over time"
    assert _SUFFIX.sub("", "Third-party supplier risk (CC9.2)") == "Third-party supplier risk"
