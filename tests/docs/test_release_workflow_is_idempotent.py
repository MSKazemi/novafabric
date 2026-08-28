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

"""A release tag has two publishers, so `release.yml` must tolerate losing.

`release.yml` fires on a `v*` tag push and creates the GitHub Release. But
`dualgit release` *prints* a `gh release create` for a human to run, deliberately,
because releases are outward-facing and are not automated here. So for any given
tag both may run, and `gh release create` is not idempotent: the loser gets
`HTTP 422 Validation Failed / Release.tag_name already exists` and the run goes
red for a release that was in fact published.

That is not hypothetical — it is **6 of the 10 most recent runs** of this
workflow (v0.98.1, v0.98.2, v0.98.3, v0.99.0, and three tags re-pushed by the
2026-08-08 history rewrite). On v0.99.0 the release was created at 12:44:20Z and
this step failed at 12:44:35Z, fifteen seconds behind. A workflow that is red on
most releases teaches everyone to ignore it, which is the whole value of a
release gate gone.

The guard is one `if gh release view ... ; then exit 0; fi`, which is exactly the
kind of line a later edit drops while "simplifying" the step. Hence this test.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/release.yml"


def _create_release_step() -> dict:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["github-release"]["steps"]
    matches = [s for s in steps if s.get("name") == "Create GitHub Release"]
    assert len(matches) == 1, (
        "expected exactly one 'Create GitHub Release' step in release.yml; "
        f"found {len(matches)}. If it was renamed, update this test with it."
    )
    return matches[0]


def _code_only(body: str) -> str:
    """Drop comment lines — the prose here names both commands it describes."""
    return "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )


def test_the_create_step_checks_for_an_existing_release_first() -> None:
    """`gh release create` must be guarded, or a second publisher turns it red."""
    body = _code_only(_create_release_step()["run"])
    assert "gh release create" in body, (
        "release.yml no longer creates a release — this test is now testing nothing"
    )
    guard = body.index("gh release view")
    create = body.index("gh release create")
    assert guard < create, (
        "`gh release create` is not guarded by a `gh release view` existence "
        "check, so a tag whose release was already published by the human "
        "following `dualgit release` will fail this step with HTTP 422."
    )


def test_the_derived_title_is_not_interpolated_into_the_shell() -> None:
    """`title` comes from a file in the repo, so `${{ }}` in `run:` would run it."""
    step = _create_release_step()
    body = step["run"]
    assert "${{" not in body, (
        "a `${{ }}` expression is interpolated into the shell script body. The "
        "release title is derived from the first line of docs/releases/<tag>.md, "
        "so that line would execute as shell. Pass it through `env:` instead."
    )
    assert "RELEASE_TITLE" in step.get("env", {}), (
        "the title should reach the script as an environment variable"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_the_guarded_step_exits_zero_when_the_release_already_exists() -> None:
    """Run the real step body against a stub `gh` that reports it exists.

    Asserting on the text above proves the guard is *written*; this proves it
    *works*. Both matter: the shape of a shell conditional is easy to get right
    on inspection and wrong in execution.
    """
    body = _create_release_step()["run"]

    def run(exists: str, tmp: Path) -> subprocess.CompletedProcess[str]:
        bin_dir = tmp / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        stub = bin_dir / "gh"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            'if [ "$1" = "release" ] && [ "$2" = "view" ]; then\n'
            f"  exit {0 if exists == 'yes' else 1}\n"
            "fi\n"
            'if [ "$1" = "release" ] && [ "$2" = "create" ]; then\n'
            f"  {'echo already-exists >&2; exit 1' if exists == 'yes' else 'exit 0'}\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        script = tmp / "step.sh"
        script.write_text("set -euo pipefail\n" + body, encoding="utf-8")
        import os

        env = dict(os.environ)
        env.update(
            PATH=f"{bin_dir}:{env['PATH']}",
            GITHUB_REF_NAME="v0.0.0-test",
            RELEASE_TITLE="a title",
            RELEASE_NOTES=str(tmp / "notes.md"),
        )
        (tmp / "notes.md").write_text("# a title\n", encoding="utf-8")
        return subprocess.run(
            ["bash", str(script)], env=env, capture_output=True, text=True, timeout=60
        )

    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        already = run("yes", tmp / "already")
        assert already.returncode == 0, (
            "the step failed on a release that already exists — this is exactly "
            f"the red run the guard exists to prevent.\n{already.stderr}"
        )
        fresh = run("no", tmp / "fresh")
        assert fresh.returncode == 0, (
            f"the step failed on a tag with no release yet.\n{fresh.stderr}"
        )
