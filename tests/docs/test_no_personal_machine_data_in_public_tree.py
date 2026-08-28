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

"""Nothing tracked publicly may carry the maintainer's machine identity.

This is the third instance of one class: **a file that was never written to be
read by strangers ends up on a public surface because the directory around it is
published wholesale.** The first was a personal ``robots.txt`` copied into the
package. The second was ``web/src/data/schemas/``, published but guarded by
nothing. The third — found 2026-08-28 — was ``examples/capsules/minimal-run/``,
the flagship example a new user opens to learn the capsule format. It recorded:

* ``HOME: /home/mohsen``, ``USER: mohsen``, ``USERNAME: mohsen``
* a full ``PATH`` naming the maintainer's Cursor extensions and Claude plugin
  cache, down to version numbers
* a build path under ``.claude/worktrees/`` in ``outputs/stdout.txt``

None of it is a credential, so no secret scanner would fire. It is a *product*
defect as much as a privacy one: the example that teaches what NovaFabric
captures demonstrated it hoovering up one laptop's developer tooling, in a tool
whose pitch is disciplined redaction.

The sweep is over ``git ls-files`` — what the **public** remote actually carries
— never over the working tree, which in this dual-git checkout also holds the
private superset. That distinction is the whole point: the earlier community
audit found 151 dead public links precisely because it tested "file exists"
instead of "tracked publicly".

⚠ **A guard that greps for a string cannot be excluded from its own sweep by
accident.** This file quotes ``/home/mohsen`` twice — once in the prose above and
once in the pattern — so it matches itself. That was invisible when the guard was
first written and run, because an uncommitted file is not in ``git ls-files``:
the sweep's scope silently widened the moment the file was committed, and a test
that had just been proven red-green went red at HEAD. Verifying a
``git ls-files``-scoped check *before* committing it does not verify the state
that ships. This file is therefore skipped by name below.

Two further exemptions, named rather than silent: ``docs/releases/`` holds dated
release notes. ``v0.6.10.md`` cites a path on the maintainer's own machine and says so
in the same sentence ("founder's machine"). Editing a shipped release note to
tidy history is worse than the disclosure, which is deliberate and labelled.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# The home directory and the bare username as a shell/YAML value. The bare word
# is too common to grep for on its own; these two forms are not.
PERSONAL = re.compile(r"/home/mohsen\b|(?<=[=:] )mohsen$|(?<=[=:] )mohsen(?=\s)")

#: This file matches its own pattern; see the docstring.
SELF = "tests/docs/test_no_personal_machine_data_in_public_tree.py"

EXEMPT_PREFIXES = ("docs/releases/", SELF)

# Binary and lockfile noise that cannot meaningfully contain prose.
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf", ".woff2")


def _public_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in out.splitlines() if not p.endswith(SKIP_SUFFIXES)]


def test_the_sweep_is_not_vacuous() -> None:
    """A wrong cwd or a detached gitdir would make the sweep below pass on nothing."""
    files = _public_files()
    assert len(files) > 1000, f"expected the public tree, got {len(files)} files"
    assert "README.md" in files


def test_no_publicly_tracked_file_carries_the_maintainers_machine_identity() -> None:
    offenders: list[str] = []
    exempted: list[str] = []

    for rel in _public_files():
        path = REPO / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in PERSONAL.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            entry = f"{rel}:{line}  {match.group(0)}"
            (exempted if rel.startswith(EXEMPT_PREFIXES) else offenders).append(entry)

    assert not offenders, (
        "publicly tracked files carry the maintainer's home directory or username. "
        "Replace with a neutral demo identity (/home/demo, USER: demo) rather than "
        "deleting the field — an example capsule with the field missing teaches the "
        "format wrong:\n  " + "\n  ".join(offenders)
    )
