"""An example script's executability must be recorded in git, not just on disk.

`examples/` is the one directory a reader clones and runs verbatim. When such a
script carries a shebang, the README tells them to execute it directly, so the
executable bit is part of the example — a 644 script fails with "Permission
denied" for every reader while working perfectly for whoever wrote it.

That asymmetry is exactly how this escaped. `examples/hpc-slurm-job/job.sbatch`
and `examples/notebook-capture/run.sh` were committed as mode 100644 while the
author's working tree held 775. Two tests already asserted the files were
executable, and both passed locally forever, because they asked the
*filesystem* — which had a bit that was never committed. CI checks out from git,
got 644, and `main` went red on three consecutive runs before anyone read the
log.

The lesson generalises past file modes: **a property that must survive a clone
has to be asserted against what git recorded, never against the working tree.**
A local `chmod` is invisible to `git status`, so nothing else in the repository
would have surfaced the difference.

The file list is derived from git, so a new shebang-carrying example is covered
the moment it is added — there is no list here to forget to update.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# git's mode for a regular file the index marks executable.
_EXECUTABLE = "100755"


def _tracked_example_blobs() -> list[tuple[str, str, str]]:
    """Return (mode, sha, path) for every tracked file under `examples/`."""
    proc = subprocess.run(
        ["git", "ls-files", "-s", "--", "examples"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip("not a git checkout (sdist or exported tree)")

    entries: list[tuple[str, str, str]] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        meta, _, path = line.partition("\t")
        mode, sha, _stage = meta.split()
        entries.append((mode, sha, path))
    return entries


def _starts_with_shebang(sha: str) -> bool:
    """True when the blob's first two bytes are `#!`.

    Reads the blob out of git rather than the working tree: the point of this
    module is that the two can disagree.
    """
    proc = subprocess.run(
        ["git", "cat-file", "blob", sha],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0 and proc.stdout[:2] == b"#!"


def test_every_shebang_example_is_executable_in_the_index() -> None:
    entries = _tracked_example_blobs()
    assert entries, "no tracked files under examples/ — the query is wrong"

    scripts = [(mode, path) for mode, sha, path in entries if _starts_with_shebang(sha)]
    assert scripts, (
        "no shebang-carrying example found; this guard would silently pass "
        "forever, so the detection above is broken"
    )

    not_executable = sorted(path for mode, path in scripts if mode != _EXECUTABLE)
    assert not not_executable, (
        "these example scripts carry a shebang but git records them as "
        "non-executable, so a fresh clone cannot run them:\n  "
        + "\n  ".join(not_executable)
        + "\n\nFix with: git update-index --chmod=+x <path>  (a local chmod is "
        "not enough — it never reaches the commit)."
    )
