"""Every workflow job must be confined to the public repository.

The private mirror carries the same ``.github/`` tree, because CI belongs to the
public surface and the tree is shared. Without a repository guard on each job,
every push to ``MSKazemi/novafabric-private`` runs the whole suite a second time:

* on 2026-09-27 the mirror had run ``CI`` five times, every one a failure, one of
  them for 40m27s, plus ``OpenSSF Scorecard`` (which only works on public repos)
  and ``docs``, also all failing;
* private-repository Actions minutes are billable, public ones are not;
* and a permanently red Actions tab is how a *real* failure hides — v0.102.0's
  three failed publish jobs sat in exactly that kind of noise.

The four publish workflows already carried this guard, which is what kept a tag
pushed to the mirror from publishing images. This asserts the same rule for every
other job, so a new workflow cannot quietly start running on the mirror.

Note for anyone adding a job whose ``if:`` uses ``||``: parenthesise it. GitHub
binds ``A || B && guard`` as ``A || (B && guard)``, which leaves the mirror
running. ``cla.yml`` documents that trap inline.
"""

from __future__ import annotations

import pathlib
import re

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

#: The public repository. Anything else — notably the private mirror — must not run.
PUBLIC_REPO_GUARD = "github.repository == 'MSKazemi/novafabric'"


def _jobs() -> list[tuple[str, str, dict]]:
    found: list[tuple[str, str, dict]] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for name, job in (doc.get("jobs") or {}).items():
            found.append((path.name, name, job or {}))
    return found


def _top_level_or(condition: str) -> bool:
    """True when an ``||`` sits outside every bracket, so the guard cannot dominate.

    Presence of the guard TEXT is not presence of the guard. GitHub binds
    ``A || B && guard`` as ``A || (B && guard)``: the string is there, the job
    still runs on the mirror. Stripping balanced groups leaves only the
    top-level operators, which is what actually decides.
    """
    stripped = condition
    while True:
        reduced = re.sub(r"\([^()]*\)", "", stripped)
        if reduced == stripped:
            return "||" in stripped
        stripped = reduced


def test_every_workflow_job_is_confined_to_the_public_repo() -> None:
    offenders = []
    for wf, name, job in _jobs():
        cond = str(job.get("if") or "")
        if PUBLIC_REPO_GUARD not in cond:
            offenders.append(f"{wf}::{name}  MISSING  if: {cond[:60] or '(none)'}")
        elif _top_level_or(cond):
            offenders.append(
                f"{wf}::{name}  INEFFECTIVE (unparenthesised ||)  if: {cond[:60]}"
            )
    assert not offenders, (
        "these workflow jobs would also run on the private mirror, burning "
        f"billable minutes and reddening that repo. Add `{PUBLIC_REPO_GUARD}` "
        "and parenthesise any `||` condition:\n  " + "\n  ".join(offenders)
    )


def test_the_sweep_is_not_vacuous() -> None:
    jobs = _jobs()
    assert len(jobs) > 30, f"expected the full workflow set, found {len(jobs)} jobs"
    names = {wf for wf, _, _ in jobs}
    assert "ci.yml" in names and "publish-image.yml" in names
