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

"""The sdist must not be able to carry private material out of this tree.

This repo is dual-git: one working tree, a public `.git` and a private
`.git-private`. The private material is deliberately **not** in `.gitignore` —
its header says so, and says it is "kept out of the public remote by the
repo-local allowlist in `.git/info/exclude`".

hatchling reads `.gitignore`. It does **not** read `.git/info/exclude`. So the
only firewall this repo has is invisible to the packaging tool. Before
`[tool.hatch.build.targets.sdist]` existed, `uv build` on a developer machine
produced a **343 MB** sdist containing `.git-private/` in full — the entire
private git history — plus `design/` and `experiments/`. Every sdist on PyPI was
built by CI from a clean public checkout and is verified clean, so nothing
leaked; but one local `uv build` and upload would have published everything.

A hand-maintained exclude list rots: the next private top-level directory is not
in it. So this derives the required set from the two gitdirs — anything the
private git tracks that the public git does not is private by definition — and
fails until it is excluded.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PRIVATE_GITDIR = ROOT / ".git-private"

pytestmark = pytest.mark.skipif(
    not PRIVATE_GITDIR.exists(),
    reason="no .git-private — a public clone has nothing private to leak",
)


def _tracked(gitdir: Path) -> set[str]:
    """Top-level path components tracked by the git at `gitdir`."""
    result = subprocess.run(
        ["git", f"--git-dir={gitdir}", f"--work-tree={ROOT}", "ls-files", "-z"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT,
    )
    assert result.returncode == 0, f"git ls-files failed: {result.stderr}"
    return {p.split("/", 1)[0] for p in result.stdout.split("\0") if p}


def _sdist_excludes() -> list[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]


def test_the_sdist_target_declares_excludes() -> None:
    """Guard the guard: no section at all is the state this test was written for."""
    assert _sdist_excludes(), (
        "[tool.hatch.build.targets.sdist] declares no excludes. hatchling cannot "
        "see .git/info/exclude, so an sdist built here carries the private tree."
    )


def test_both_gitdirs_are_readable() -> None:
    """Guard the guard: an empty tracked set would make the check vacuous."""
    assert len(_tracked(ROOT / ".git")) > 10, "public git tracked almost nothing"
    assert len(_tracked(PRIVATE_GITDIR)) > 10, "private git tracked almost nothing"


def test_every_private_only_top_level_path_is_excluded_from_the_sdist() -> None:
    """Anything the private git tracks and the public git does not must be excluded."""
    private_only = _tracked(PRIVATE_GITDIR) - _tracked(ROOT / ".git")
    assert private_only, (
        "the private git tracks nothing the public git does not — either the "
        "dual-git split has collapsed, or this test is now vacuous"
    )
    excluded = {e.lstrip("/").rstrip("/") for e in _sdist_excludes()}
    missing = sorted(p for p in private_only if p not in excluded)
    assert not missing, (
        "these top-level paths are tracked ONLY by the private git, so they are "
        "private by definition, but nothing in "
        "[tool.hatch.build.targets.sdist].exclude keeps them out of the sdist:\n"
        f"  {missing}\n"
        "hatchling does not read .git/info/exclude, so a local `uv build` will "
        "package them. Add each to the exclude list in pyproject.toml."
    )


def test_the_private_gitdir_itself_is_excluded() -> None:
    """`.git-private/` is tracked by neither git, so the derived check misses it.

    It is also the worst single thing that could ship: the whole private history,
    a ~150 MB pack file. It was in the 343 MB sdist measured before the fix.
    """
    excluded = {e.lstrip("/").rstrip("/") for e in _sdist_excludes()}
    assert ".git-private" in excluded, (
        ".git-private is not in the sdist exclude list. It is tracked by neither "
        "gitdir, so no derived rule covers it — it must be named explicitly."
    )
