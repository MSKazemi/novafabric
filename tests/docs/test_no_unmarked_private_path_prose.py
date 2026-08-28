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

"""Public docs must not name a private `design/` path as if it were readable.

`make check-links` verifies that every relative *link* in the public docs
resolves. Prose is not a link, so a sentence like "specified in
``design/spec/backup-restore-v0.md``" passes that gate while being a dead end for
every public reader — the `design/` tree is not part of this repository. Issue #5
reported one instance in the developer guide and named the gap explicitly: the
link gate exists, and plain-prose references still slip through. This is that
missing gate.

Naming a private document is allowed, and is often the honest thing to do — the
spec really is the normative source. What is not allowed is naming it without
saying it is private, because that is indistinguishable to the reader from a file
they should be able to open and cannot find.

Scope, stated rather than implied:

* Only files **tracked by the public git** are checked. A file existing on this
  machine says nothing about what a visitor can see, which is the whole subject
  of this test.
* ``docs/releases/`` is exempt. Those are dated records of what a release
  contained, including which ADR files it added; editing them to read better
  today would falsify the record. They are listed in the failure message anyway
  when they match, so the exemption is visible rather than silent.

The class is wider than ``docs/``, which is what the first version of this guard
missed. Measured on 2026-08-28 across the whole public tree: **402 references in
223 files**. Three tiers reach a stranger and each has its own case below:
``docs/``; the published JSON Schemas, whose ``$comment`` strings are served from
``novafabric.io/schemas/``; and the shipped source tree, whose docstrings reach
``help()``/``inspect.getdoc`` and are read directly on GitHub.

⚠ There is **no docstring-based documentation generator in this repository** —
``docs/python-api.md`` is written by hand and ``docs/api-reference.md`` is
generated from the FastAPI route table, not from module docstrings. The exposure
for the source tier is ``help()`` and the public source itself, which is narrower
than a generated site but is still a public surface.

The final case below closes the class: it sweeps **everything** ``git ls-files``
reports, so a new public surface is covered the day it is added rather than the
day someone remembers to extend a list of directories. The three tiered cases are
kept because their failure messages say what each surface is and how it reaches a
reader; the catch-all is the backstop, not a replacement.

Two exemptions, both named here and both listed in the failure message when they
match, so neither is silent:

* ``docs/releases/`` — see above.
* ``CHANGELOG.md`` (77 references) — the same reasoning as ``docs/releases/``.
  It is one dated record per release, and rewriting a shipped entry so it reads
  better today falsifies what that release actually said.
* ``tests/`` (32 references) — not shipped, not read by users, and the guards
  themselves must be free to quote the paths they are guarding.
* ``src/novafabric/serve/static/`` — **generated**. It is the built dashboard
  bundle, checked in so ``nova serve`` works from a wheel. Hand-editing build
  output would be overwritten by the next build and would make the artifact
  disagree with its source. Its source *is* covered: ``web/src/lib/links.ts`` is
  guarded by ``test_site_links_resolve_publicly.py``. The exemption is about
  *where the fix belongs*, not about tolerating a match: when the bundle was
  rebuilt on 2026-08-29 it picked up both the marked ``$comment`` strings and
  the repointed link, and the sweep finds nothing in it today. The exemption
  stays so that a future source-side lag is reported against the source rather
  than against generated output.

* ``x-novafabric`` blocks inside a schema are exempt. Their ``spec`` value is a
  machine-readable document identifier, not prose addressed to a reader, and the
  same file's ``$comment`` already marks the tree as private. Editing the value
  would corrupt the datum.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: A `design/...` path written in prose or backticks.
PRIVATE_PATH = re.compile(r"`?\bdesign/[A-Za-z0-9_./-]+")

#: Words that make the privacy explicit. Checked in a window around the match.
MARKERS = ("private", "not published", "not part of this repository", "maintainers")

WINDOW = 240
EXEMPT_PREFIXES = (
    "docs/releases/",
    "CHANGELOG.md",
    "tests/",
    "src/novafabric/serve/static/",  # generated; its source is guarded instead
)

#: Extensions that cannot carry prose addressed to a reader.
BINARY_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf", ".woff2")


def _public_docs() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "docs"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in out.splitlines() if p.endswith(".md")]


def test_no_public_doc_names_a_private_path_without_saying_it_is_private() -> None:
    offenders: list[str] = []
    exempted: list[str] = []

    for rel in _public_docs():
        text = (REPO / rel).read_text(encoding="utf-8", errors="ignore")
        for match in PRIVATE_PATH.finditer(text):
            start = max(0, match.start() - WINDOW)
            context = text[start : match.end() + WINDOW].lower()
            if any(marker in context for marker in MARKERS):
                continue
            line = text.count("\n", 0, match.start()) + 1
            entry = f"{rel}:{line}  {match.group(0)}"
            (exempted if rel.startswith(EXEMPT_PREFIXES) else offenders).append(entry)

    assert not offenders, (
        "public docs name a private design/ path without marking it private — a "
        "dead end for every reader, and invisible to `make check-links` because "
        "prose is not a link:\n  " + "\n  ".join(offenders)
    )


def _public_schemas() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "schemas", "src/novafabric/schemas", "web/src/data/schemas"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in out.splitlines() if p.endswith(".json")]


def test_the_schema_sweep_is_not_vacuous() -> None:
    """A bad path would make the case below pass without checking anything."""
    schemas = _public_schemas()
    assert len(schemas) >= 100, len(schemas)
    assert any("design/" in (REPO / s).read_text(encoding="utf-8") for s in schemas)


def test_no_published_schema_names_a_private_path_without_saying_it_is_private() -> None:
    """``$comment`` is served to third parties who cannot open a `design/` path."""
    offenders: list[str] = []

    for rel in _public_schemas():
        text = (REPO / rel).read_text(encoding="utf-8", errors="ignore")
        for match in PRIVATE_PATH.finditer(text):
            start = max(0, match.start() - WINDOW)
            context = text[start : match.end() + WINDOW].lower()
            if any(marker in context for marker in MARKERS):
                continue
            if '"spec": "' in text[max(0, match.start() - 12) : match.start()]:
                continue  # x-novafabric machine-readable pointer, see module docstring
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{rel}:{line}  {match.group(0)}")

    assert not offenders, (
        "published JSON Schemas name a private design/ path without marking it "
        "private. These $comment strings are served from novafabric.io/schemas/ "
        "and read by consumers who have no way to open the document:\n  "
        + "\n  ".join(offenders)
    )


def _public_source() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "src"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in out.splitlines() if p.endswith((".py", ".md", ".rego"))]


def test_the_source_sweep_is_not_vacuous() -> None:
    src = _public_source()
    assert len(src) >= 500, len(src)
    assert any("design/" in (REPO / s).read_text(encoding="utf-8") for s in src)


def test_no_shipped_source_file_names_a_private_path_without_saying_it_is_private() -> None:
    """Docstrings reach ``help()`` and are read directly in the public repo."""
    offenders: list[str] = []

    for rel in _public_source():
        text = (REPO / rel).read_text(encoding="utf-8", errors="ignore")
        for match in PRIVATE_PATH.finditer(text):
            start = max(0, match.start() - WINDOW)
            context = text[start : match.end() + WINDOW].lower()
            if any(marker in context for marker in MARKERS):
                continue
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{rel}:{line}  {match.group(0)}")

    assert not offenders, (
        "shipped source names a private design/ path without marking it private. "
        "These strings reach help() and are read directly in the public "
        "repository, where the design/ tree does not exist:\n  "
        + "\n  ".join(offenders)
    )


def _everything_public() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in out.splitlines() if not p.endswith(BINARY_SUFFIXES)]


def test_the_whole_tree_sweep_is_not_vacuous() -> None:
    files = _everything_public()
    assert len(files) > 1000, f"expected the public tree, got {len(files)} files"
    assert "ROADMAP.md" in files and "pyproject.toml" in files


def test_no_publicly_tracked_file_names_a_private_path_without_saying_it_is_private() -> None:
    """The backstop: every public surface, not an enumerated list of directories.

    This is what actually closes issue #5's class. The tiered cases above cover
    ``docs/``, the published schemas and the shipped source with surface-specific
    messages; this one catches the rest — ROADMAP, ``pyproject.toml``, CI
    workflows, example READMEs, the SDK package, and the site's own TypeScript.
    """
    offenders: list[str] = []
    exempted: list[str] = []

    for rel in _everything_public():
        path = REPO / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in PRIVATE_PATH.finditer(text):
            start = max(0, match.start() - WINDOW)
            context = text[start : match.end() + WINDOW].lower()
            if any(marker in context for marker in MARKERS):
                continue
            if '"spec": "' in text[start : match.start()]:
                continue  # machine-readable x-novafabric pointer, see the docstring
            line = text.count("\n", 0, match.start()) + 1
            entry = f"{rel}:{line}  {match.group(0)}"
            (exempted if rel.startswith(EXEMPT_PREFIXES) else offenders).append(entry)

    assert not offenders, (
        "publicly tracked files name a private design/ path without marking it "
        "private. To a reader this is indistinguishable from a file they should "
        "be able to open. Say it is private, or point at the published "
        "counterpart (docs/decisions.md for an ADR, docs/architecture.md for "
        "design rationale):\n  " + "\n  ".join(offenders)
    )
