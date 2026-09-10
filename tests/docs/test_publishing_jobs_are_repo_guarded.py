# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Every job that publishes OUTWARD must refuse to run outside the public repo.

This repository is dual-git: one working tree, two independent gitdirs pushing to
`MSKazemi/novafabric` (public) and `MSKazemi/novafabric-private` (mirror). A tag
reaches BOTH by design, so a tag-triggered publish job runs twice unless it checks
which repository it is in.

`release.yml` did not check, and created a duplicate GitHub Release for every tag —
`v0.101.0` exists on both repositories, three seconds apart. Its three siblings
(`publish-pypi`, `publish-image`, `publish-chart`) already carried the guard, which is
exactly why nobody noticed the fourth was missing: the family looked protected.

The set of publishing jobs is DERIVED from what the steps actually do, never
hand-listed. A hand-written mirror of "the publish workflows" drifts the moment a
fifth one is added — which is the failure this test exists to prevent, so it must not
reproduce it. Jobs that merely *build* are out of scope: running a build on the mirror
wastes minutes but ships nothing, and a guard that flags harmless jobs gets ignored.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

CANONICAL_REPO = "MSKazemi/novafabric"

# A step publishes outward if it uses one of these actions, or runs one of these
# commands. Matched against `uses:` prefixes and `run:` bodies respectively.
# NB: `sigstore/cosign-installer` is deliberately ABSENT. Installing cosign ships
# nothing; it was in this tuple first and flagged `release-toolchain.yml`, a job whose
# whole purpose is running the publish toolchain WITHOUT publishing ("no push", and its
# build-push step sets `push: false`). The signing that does publish is `cosign sign`,
# caught below as a command. Detect the act, not the tool.
PUBLISH_ACTIONS = (
    "pypa/gh-action-pypi-publish",
    "docker/build-push-action",
)
PUBLISH_COMMANDS = (
    "gh release create",
    "helm push",
    "cosign sign",
    "docker push",
    "twine upload",
)


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _publishes(job: dict[str, Any]) -> list[str]:
    """Return the step names in `job` that push something to the outside world."""
    hits = []
    for step in job.get("steps") or []:
        if not isinstance(step, dict):
            continue
        name = step.get("name") or step.get("uses") or "<unnamed step>"
        uses = str(step.get("uses") or "")
        run = str(step.get("run") or "")
        if any(uses.startswith(a) for a in PUBLISH_ACTIONS):
            # build-push-action only publishes when push is truthy.
            if uses.startswith("docker/build-push-action"):
                if str((step.get("with") or {}).get("push", "")).lower() != "true":
                    continue
            hits.append(name)
        elif any(c in run for c in PUBLISH_COMMANDS):
            hits.append(name)
    return hits


def _publishing_jobs() -> list[tuple[str, str, list[str]]]:
    found = []
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        for job_name, job in (_load(wf).get("jobs") or {}).items():
            if isinstance(job, dict) and (steps := _publishes(job)):
                found.append((wf.name, job_name, steps))
    return found


def test_the_detector_finds_the_known_publishing_jobs() -> None:
    """Non-vacuity: if this finds nothing, every assertion below passes for free.

    Printed rather than pinned to an exact set — a new publish workflow should make
    this test *cover more*, not fail.
    """
    found = _publishing_jobs()
    for wf, job, steps in found:
        print(f"  {wf}::{job} -> {steps}")
    assert len(found) >= 4, (
        f"expected the four known publish jobs (pypi, image, chart, release), "
        f"found {len(found)}: {[(w, j) for w, j, _ in found]}. "
        "If a workflow was renamed, update PUBLISH_ACTIONS/PUBLISH_COMMANDS — do not "
        "lower this floor."
    )


@pytest.mark.parametrize(
    ("workflow", "job"),
    [(w, j) for w, j, _ in _publishing_jobs()],
    ids=[f"{w}::{j}" for w, j, _ in _publishing_jobs()],
)
def test_every_publishing_job_refuses_to_run_outside_the_public_repo(
    workflow: str, job: str
) -> None:
    condition = str((_load(WORKFLOWS / workflow).get("jobs") or {})[job].get("if") or "")
    assert f"github.repository == '{CANONICAL_REPO}'" in condition, (
        f"{workflow}::{job} publishes outward but carries no repository guard "
        f"(if: {condition!r}). A tag reaches the private mirror too, so this job "
        f"would publish — or duplicate a release — from novafabric-private.\n"
        f"Add:  if: github.repository == '{CANONICAL_REPO}'"
    )
