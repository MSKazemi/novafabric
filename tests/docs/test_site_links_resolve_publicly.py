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

"""Every ``githubBlob()`` link on the site must name a path the public repo tracks.

``web/src/lib/links.ts`` turns a repo-relative path into an absolute
``github.com/MSKazemi/novafabric/blob/main/<path>`` URL rendered into the built
site. Until 2026-08-28 two of its three constants pointed into ``design/``, which
publishes **zero** files — so ``/why``'s "non-goals doc" link, introduced in the
sentence calling it *"the load-bearing constraint"*, was a 404 for every visitor.
The file's own header explained the 404 away as temporary ("until the repo is
public"); the repo had been public for months.

Two lessons the project has already paid for, both applying here:

* **For any public-facing check, test "tracked by the public git", never "file
  exists".** The community-readiness audit found 151 dead public links exactly
  this way. In this dual-git checkout the working tree also holds the private
  superset, so ``Path.exists()`` returns True for every one of these targets.
* **A stale comment is worse than no comment** — it converts a defect into an
  accepted condition and stops anyone looking again.

The check is offline and deliberately so: it asks git what the repository
contains rather than asking github.com, so it works in CI, in a clone with no
network, and without spending a request per link.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LINKS_TS = REPO / "web" / "src" / "lib" / "links.ts"

# githubBlob('some/path')  — single or double quoted.
BLOB_CALL = re.compile(r"githubBlob\(\s*['\"]([^'\"]+)['\"]\s*\)")


def _tracked_publicly() -> set[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return set(out.splitlines())


def test_the_sweep_is_not_vacuous() -> None:
    """A renamed file or a changed helper name would silently check nothing."""
    assert LINKS_TS.is_file(), LINKS_TS
    targets = BLOB_CALL.findall(LINKS_TS.read_text(encoding="utf-8"))
    assert len(targets) >= 3, f"expected several githubBlob() links, found {targets}"
    assert len(_tracked_publicly()) > 1000


def test_every_site_github_link_points_at_a_publicly_tracked_path() -> None:
    tracked = _tracked_publicly()
    # A link may name a directory (e.g. 'schemas'); that resolves on GitHub as
    # long as the public repo tracks anything beneath it.
    prefixes = {p.rsplit("/", 1)[0] for p in tracked if "/" in p}

    text = LINKS_TS.read_text(encoding="utf-8")
    offenders: list[str] = []
    for match in BLOB_CALL.finditer(text):
        target = match.group(1)
        if target in tracked:
            continue
        if any(p == target or p.startswith(target + "/") for p in prefixes):
            continue
        line = text.count("\n", 0, match.start()) + 1
        offenders.append(f"web/src/lib/links.ts:{line}  githubBlob({target!r})")

    assert not offenders, (
        "the site builds GitHub links to paths the public repository does not "
        "track, so they 404 for every visitor. `design/` publishes nothing — link "
        "to the published counterpart instead (docs/decisions.md for an ADR, "
        "docs/architecture.md for design rationale):\n  " + "\n  ".join(offenders)
    )
