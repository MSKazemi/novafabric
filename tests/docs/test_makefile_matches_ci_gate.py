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

"""The documented release gate is the gate CI actually runs (BL-043).

`CLAUDE.md` and `CONTRIBUTING.md` tell a contributor that `make test-par` is the
release gate. That is only true while its command matches CI's `unit` job, and
it had drifted three ways: `test-par` ran a wider scope than CI (it did not
exclude `tests/integration`) and omitted the `--cov-fail-under=90` floor CI
enforces, while `test-fast` used `--dist=load` against CI's `--dist=loadgroup`.

The distribution one was not cosmetic. It is how BL-042 reached `main`: a race in
a session-scoped fixture that only manifests under a particular worker
distribution passed every local run and failed on CI, twice, on different tests.

So this asserts the two are the same command rather than trusting a comment that
says they are. Both files are parsed for the real invocation — nothing here
restates the flags, because a third copy of a fact is a third thing to drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_ROOT = Path(__file__).resolve().parents[2]
_MAKEFILE = _ROOT / "Makefile"
_CI = _ROOT / ".github" / "workflows" / "ci.yml"

#: Flags that must agree between the Makefile's release gate and CI's unit job.
#: Only the ones that change *what is verified* — formatting flags like `-q` are
#: deliberately not compared, because they change nothing about the outcome.
_SEMANTIC_FLAGS = (
    "--ignore=tests/integration",
    "--dist=loadgroup",
    "--cov=novafabric",
    "--cov-fail-under=90",
)


def _makefile_recipe(target: str) -> str:
    """Return *target*'s recipe as one whitespace-normalised line."""
    text = _MAKEFILE.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(target)}:\n((?:\t.*\n)+)", text, re.MULTILINE)
    assert match, f"no recipe found for `{target}` in {_MAKEFILE}"
    body = match.group(1).replace("\\\n", " ")
    return " ".join(body.split())


def _ci_unit_command() -> str:
    """Return the `unit` job's pytest invocation from the CI workflow."""
    workflow = yaml.safe_load(_CI.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["unit"]["steps"]
    runs = [
        " ".join(str(step["run"]).split())
        for step in steps
        if "run" in step and "pytest" in str(step["run"])
    ]
    assert len(runs) == 1, f"expected exactly one pytest step in CI's unit job, got {len(runs)}"
    return runs[0]


def test_release_gate_matches_ci_semantically() -> None:
    """`make test-par` verifies what CI verifies — same scope, same floor."""
    recipe = _makefile_recipe("test-par")
    ci = _ci_unit_command()

    missing = [flag for flag in _SEMANTIC_FLAGS if flag in ci and flag not in recipe]
    assert not missing, (
        "make test-par is documented as the release gate but does not run what "
        f"CI's unit job runs — missing {missing}.\n"
        f"  make test-par: {recipe}\n"
        f"  CI unit      : {ci}"
    )


def test_release_gate_adds_no_hidden_scope() -> None:
    """…and it does not verify *less* by quietly skipping a tier CI runs.

    An extra `--ignore` in the Makefile would make the local gate pass while CI
    exercises the skipped tier — the same class of gap in the other direction.
    """
    recipe = _makefile_recipe("test-par")
    ci = _ci_unit_command()

    local_ignores = set(re.findall(r"--ignore=\S+", recipe))
    ci_ignores = set(re.findall(r"--ignore=\S+", ci))
    extra = local_ignores - ci_ignores
    assert not extra, (
        f"make test-par skips tiers CI does not: {sorted(extra)} — the local "
        "gate would pass while CI exercises them"
    )


def test_fast_loop_uses_the_same_distribution_as_ci() -> None:
    """The dev loop distributes tests the way CI does.

    `--dist=loadgroup` is not a tuning knob here (CI's own comment says so): the
    testcontainers tier pins itself to one `xdist_group`. Beyond that, matching
    the distribution is what stops an order- or timing-dependent failure from
    being invisible until CI — which is exactly how BL-042 landed.
    """
    recipe = _makefile_recipe("test-fast")
    assert "--dist=loadgroup" in recipe, (
        "make test-fast must use CI's distribution; a different one hides "
        "order- and timing-dependent failures until they reach CI"
    )


def test_fast_loop_only_skips_the_infra_heavy_tiers() -> None:
    """`test-fast` may trade scope for speed — but only the documented tiers.

    It is allowed to skip more than CI (that is its purpose), so this pins the
    exception list instead of forbidding it, and a new skip has to be added here
    deliberately.
    """
    recipe = _makefile_recipe("test-fast")
    allowed = {"--ignore=tests/integration", "--ignore=tests/metadata_store"}
    actual = set(re.findall(r"--ignore=\S+", recipe))
    assert actual <= allowed, (
        f"make test-fast skips undocumented tiers: {sorted(actual - allowed)}. "
        "Speed is a fair trade for the infra-heavy tiers; anything else makes "
        "the dev loop quietly weaker than it looks."
    )
