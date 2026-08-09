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

"""The release smoke test exercises the versions the release actually uses.

`publish-image.yml` and `publish-chart.yml` fire only on a `v*` tag, so nothing
on a pull request runs them. `release-toolchain.yml` exists to close that gap: it
runs the same toolchain with publishing disabled, on every pull request that
touches the publish path.

That only works while the two carry the SAME action versions. If the publish
workflows move and the smoke test does not, the smoke test keeps passing against
versions no release will ever use — a green check that proves nothing, which is
strictly worse than no check, because it reads as coverage.

This is not hypothetical. Dependabot #58 raised seven action majors across the
image build and cosign signing path with a completely green check run, because
every affected action lived only in the tag-triggered workflows. The next release
tag would have been their first execution.

So this asserts parity by parsing both sides. Nothing here restates a version
number — a third copy of a fact is a third thing to drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS = _ROOT / ".github" / "workflows"
_PUBLISH = (_WORKFLOWS / "publish-image.yml", _WORKFLOWS / "publish-chart.yml")
_SMOKE = _WORKFLOWS / "release-toolchain.yml"

#: Actions in the publish workflows that the smoke test deliberately does NOT
#: run, each with the reason it cannot be. These are decisions, not duplicated
#: facts — a new entry here is a claim that something is untestable on a pull
#: request, and should be argued for in review.
_EXEMPT: dict[str, str] = {
    "docker/login-action": (
        "needs real registry credentials, which must never be exposed to a pull request"
    ),
    "actions/attest-build-provenance": (
        "needs the OIDC identity of a tag run to attest a published digest"
    ),
    "aquasecurity/trivy-action": (
        "scans the image after it is pushed; there is no published image on a pull request"
    ),
    "actions/upload-artifact": (
        "uploads the trivy report for the release record; reporting only, it cannot break a release"
    ),
}

_USES = re.compile(r"^\s*(?:-\s*)?uses:\s*(?P<action>[^@\s]+)@(?P<version>\S+)", re.M)


def _actions(path: Path) -> dict[str, set[str]]:
    """Map action name -> the set of versions `path` pins it to."""
    assert path.is_file(), f"{path} is missing"
    found: dict[str, set[str]] = {}
    for m in _USES.finditer(path.read_text(encoding="utf-8")):
        found.setdefault(m["action"], set()).add(m["version"])
    assert found, f"parsed no `uses:` from {path} — the parser is broken"
    return found


def _publish_actions() -> dict[str, set[str]]:
    merged: dict[str, set[str]] = {}
    for path in _PUBLISH:
        for action, versions in _actions(path).items():
            merged.setdefault(action, set()).update(versions)
    return merged


def test_smoke_test_is_wired_to_the_publish_path() -> None:
    """The smoke test must trigger on changes to the workflows it guards."""
    doc = yaml.safe_load(_SMOKE.read_text(encoding="utf-8"))
    # PyYAML parses a bare `on:` key as the boolean True.
    triggers = doc.get("on") or doc.get(True)
    assert triggers, "release-toolchain.yml declares no triggers"

    pr_paths = set(triggers["pull_request"]["paths"])
    for path in (*_PUBLISH, _SMOKE):
        rel = path.relative_to(_ROOT).as_posix()
        assert rel in pr_paths, (
            f"{rel} is not in release-toolchain.yml's pull_request paths, so a "
            f"change to it would not run the smoke test that guards it"
        )


def test_every_release_action_is_smoke_tested_or_exempt() -> None:
    """No action reaches a release tag without either coverage or a reason."""
    uncovered = sorted(set(_publish_actions()) - set(_actions(_SMOKE)) - set(_EXEMPT))
    assert not uncovered, (
        "these actions run at release time but are neither exercised by "
        f"release-toolchain.yml nor listed in _EXEMPT: {uncovered}. Add them to "
        "the smoke test, or add an entry to _EXEMPT saying why they cannot run "
        "on a pull request."
    )


def test_smoke_test_pins_the_same_versions_as_the_publish_workflows() -> None:
    """A version the smoke test proves must be the version a release uses."""
    publish, smoke = _publish_actions(), _actions(_SMOKE)

    drift = {
        action: (sorted(publish[action]), sorted(versions))
        for action, versions in smoke.items()
        if action in publish and publish[action] != versions
    }
    assert not drift, (
        "release-toolchain.yml pins different versions than the publish "
        f"workflows, so its green run proves nothing about a release: {drift}"
    )


def test_exemptions_are_still_real() -> None:
    """An exemption for an action no longer used is stale — drop it."""
    publish = _publish_actions()
    stale = sorted(set(_EXEMPT) - set(publish))
    assert not stale, (
        f"_EXEMPT excuses actions the publish workflows no longer use: {stale}. "
        "Remove them so the list stays a live description of what is untested."
    )
