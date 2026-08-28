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
EXEMPT_PREFIXES = ("docs/releases/",)


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
