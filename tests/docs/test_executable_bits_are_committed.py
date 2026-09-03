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

"""A file with a shebang must be committed executable — checked in git, not on disk.

**Why this is checked against git rather than the filesystem.** Both gitdirs here set
``core.fileMode = false``, which tells git to ignore the worktree's executable bit
entirely. That is a reasonable setting on a tree shared across filesystems, and it has one
brutal consequence: ``chmod +x`` locally changes nothing git will ever record, and nothing
in ``git status`` reveals the gap. A script can be executable on every developer's machine
and non-executable for every person who clones the repo, forever, silently.

That is not hypothetical. On 2026-09-03, CI on ``main`` was red on two assertions —
``job.sbatch must be executable`` and ``run.sh must be executable`` — while the identical
tests passed locally. The files were ``100755`` in the local index and ``100644`` in the
pushed tree. A sweep found **34** tracked files with a shebang and no executable bit,
including all three of this repo's own git hooks (``scripts/hooks/*.sh``): a fresh clone
could not run its own pre-push gate.

A shebang is a declaration that the file is meant to be executed directly. This test holds
the tree to that declaration.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Files that carry a shebang but are deliberately never executed directly. Empty today —
#: kept as the documented escape hatch so the fix for a future exception is to justify it
#: here rather than to delete the test.
ALLOWED_NON_EXECUTABLE: frozenset[str] = frozenset()


def _tracked_modes() -> dict[str, str]:
    """``path -> git mode`` for every tracked file, read from the index.

    ``git ls-files -s`` is the authority: it reports what git will hand to anyone who
    clones, which is exactly what ``core.fileMode = false`` hides from a ``stat`` call.
    """
    out = subprocess.run(
        ["git", "ls-files", "-s"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    modes: dict[str, str] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) >= 1 and path:
            modes[path] = parts[0]
    return modes


def _has_shebang(path: pathlib.Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return False


def _shebang_files() -> list[tuple[str, str]]:
    """``(path, git mode)`` for every tracked file starting with ``#!``."""
    found: list[tuple[str, str]] = []
    for path, mode in _tracked_modes().items():
        if path in ALLOWED_NON_EXECUTABLE:
            continue
        full = REPO_ROOT / path
        if full.is_file() and _has_shebang(full):
            found.append((path, mode))
    return sorted(found)


SHEBANG_FILES = _shebang_files()


def test_the_sweep_actually_finds_shebang_files() -> None:
    """Without this, a broken sweep makes the assertion below vacuously true.

    The repo has dozens of shell and Python entry points; finding almost none would mean
    the git call or the shebang read is broken, not that the tree is clean.
    """
    assert len(SHEBANG_FILES) >= 20, (
        f"only {len(SHEBANG_FILES)} shebang file(s) discovered — the sweep is broken, "
        "and the executable-bit check below would pass over nearly nothing"
    )


@pytest.mark.skipif(not SHEBANG_FILES, reason="no shebang files discovered")
def test_every_shebang_file_is_committed_executable() -> None:
    non_exec = [path for path, mode in SHEBANG_FILES if mode != "100755"]

    assert not non_exec, (
        f"{len(non_exec)} tracked file(s) start with a shebang but are committed "
        "non-executable, so anyone who clones this repo gets a script they cannot run:\n"
        + "\n".join(f"  {p}" for p in non_exec)
        + "\n\nFix with:\n    git update-index --chmod=+x <paths>\n"
        "then commit. ⚠ `chmod +x` alone does NOT work here — core.fileMode is false in "
        "both gitdirs, so git ignores the worktree bit and `git status` stays clean while "
        "the pushed tree keeps 100644."
    )


def test_core_filemode_is_false_which_is_why_this_test_reads_git() -> None:
    """Pins the premise. If someone sets core.fileMode=true, a plain `stat` check would
    start working and this test's indirection would look like pointless ceremony — so the
    reason is asserted, not just written in the docstring above."""
    out = subprocess.run(
        ["git", "config", "core.fileMode"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    assert out in ("false", ""), (
        f"core.fileMode is {out!r}. If it is now true, the worktree bit is authoritative "
        "again and this test can be simplified — but check both gitdirs first."
    )
