#!/usr/bin/env python3
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

"""Block a release until CI has passed on the exact commit being released (ADR-0316).

v0.106.0 was tagged, built, signed and published (PyPI, GitHub Release) while CI's
``unit`` job was red on that commit: the four tag-triggered workflows never
looked at CI. Each now ``needs:`` a job that runs this script, which waits for
the ``ci.yml`` run on ``$GITHUB_SHA`` to finish and **fails closed** unless it
concluded ``success``:

* no CI run for the commit (yet)  -> wait out a short grace, then fail
* CI still queued / running       -> keep waiting, up to ``--timeout``
* CI finished, not ``success``    -> fail, naming the run
* CI finished ``success``         -> pass

Stdlib only; talks to the REST API with the job's ``GITHUB_TOKEN`` (needs
``actions: read``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

API = "https://api.github.com"

Fetch = Callable[[str], dict[str, Any]]


@dataclass(frozen=True)
class Verdict:
    """``state`` is ``pass``, ``fail`` or ``wait``."""

    state: str
    message: str


def verdict(runs: list[dict[str, Any]], *, waited: float, grace: float) -> Verdict:
    """Decide from the ``ci.yml`` runs recorded for one commit."""
    if not runs:
        if waited < grace:
            return Verdict("wait", "no CI run for this commit yet")
        return Verdict(
            "fail",
            "no CI run exists for this commit (push CI runs only on the pushed head). "
            "Run it with `gh workflow run ci.yml --ref <tag>`, then re-run this workflow.",
        )
    # The newest attempt is the one that counts: a re-run supersedes a failure.
    latest = max(runs, key=lambda r: (r.get("created_at") or "", r.get("id") or 0))
    url = latest.get("html_url", "")
    if latest.get("status") != "completed":
        return Verdict("wait", f"CI is {latest.get('status')}: {url}")
    if latest.get("conclusion") == "success":
        return Verdict("pass", f"CI passed on this commit: {url}")
    return Verdict(
        "fail",
        f"CI concluded {latest.get('conclusion')!r} on this commit: {url}\n"
        "Fix it (or re-run it if it was cancelled or flaked), then re-run this job.",
    )


def _github_fetch(token: str) -> Fetch:
    def fetch(path: str) -> dict[str, Any]:
        request = urllib.request.Request(  # noqa: S310 - fixed https API host
            f"{API}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return dict(json.load(response))

    return fetch


def wait_for_green(
    fetch: Fetch,
    repo: str,
    sha: str,
    *,
    workflow: str,
    timeout: float,
    grace: float,
    interval: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Verdict:
    start = clock()
    while True:
        data = fetch(f"/repos/{repo}/actions/workflows/{workflow}/runs?head_sha={sha}&per_page=50")
        waited = clock() - start
        result = verdict(list(data.get("workflow_runs", [])), waited=waited, grace=grace)
        if result.state != "wait":
            return result
        if waited >= timeout:
            return Verdict("fail", f"gave up after {timeout:.0f}s — {result.message}")
        print(f"require-green-ci: {result.message}; checking again in {interval:.0f}s", flush=True)
        sleep(interval)


# ---------------------------------------------------------------------------
# Proposed release verdict (issue #219) -- DESIGN ONLY.
#
# Nothing below is called by ``main()`` or by any workflow: the production gate is
# still exactly the CI-only decision above. This is the pure decision the gate would
# grow into (docs/release-verdict-design.md): given facts about a release candidate
# and the run/job records GitHub returned for it, answer PASS or say why not. It does
# no I/O -- callers fetch, ``parse_runs`` / ``parse_jobs`` validate, ``decide_release``
# decides -- so every branch is testable offline and nothing here can publish.
#
# Why it exists: v0.109.0 was published while ``Collector CI`` (its ``govulncheck``
# job) had failed on the very same commit, because the gate asked about ``ci.yml``
# only. Only PASS authorizes a release; every other state blocks.
# ---------------------------------------------------------------------------
CANONICAL_REPO = "MSKazemi/novafabric"
DEFAULT_BRANCH = "main"
_TAG_REF = re.compile(r"^refs/tags/(v\d+\.\d+\.\d+)$")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
#: GitHub's compare(base=<release sha>, head=main) status when main contains the commit.
_MAIN_CONTAINS = frozenset({"ahead", "identical"})
_PROMOTION_OK = "none"
_SEVERITY = {"PASS": 0, "PENDING": 1, "FAIL": 2, "BLOCK": 3, "INVALID": 4}


class EvidenceError(Exception):
    """Evidence could not be fetched or is not what GitHub's API documents: never a pass."""


@dataclass(frozen=True)
class CheckSpec:
    """One entry of the release manifest.

    ``level`` is ``mandatory`` (must pass) or ``informational`` (reported, never blocks and
    never stands in for a mandatory check). ``implemented=False`` marks a check whose
    owning issue has not landed: it can never pass, so the manifest cannot authorize a
    release until it exists. ``on_failure`` is the verdict a failed or absent check
    produces: ``FAIL`` for source checks, ``BLOCK`` for candidate-artifact gates.
    There is deliberately no waiver field: a waiver lives inside the check itself (the
    audit scripts expire theirs) and the verdict only sees the check's conclusion.
    """

    key: str
    workflow_path: str
    level: str = "mandatory"
    required_jobs: tuple[str, ...] = ()
    events: tuple[str, ...] = ("push", "workflow_dispatch")
    all_jobs_must_pass: bool = True
    max_age_s: int | None = None
    on_failure: str = "FAIL"
    implemented: bool = True
    owner_issue: str = ""


_WF = ".github/workflows/"
_CI_JOBS = (
    "lint",
    *(f"unit ({n}/6)" for n in range(1, 7)),
    "coverage",
    "typecheck",
    "seal-latency-gate",
    "capture-overhead-gate",
    "web",
    "integration",
)
_AUDIT_EVENTS = ("push", "schedule", "workflow_dispatch")
_DAY = 24 * 3600

#: The release manifest, version 1 (design). Edit it only through review: its digest is
#: what an owner authorization binds to, and what a candidate cannot override.
RELEASE_POLICY_V1: tuple[CheckSpec, ...] = (
    CheckSpec("ci", f"{_WF}ci.yml", required_jobs=_CI_JOBS),
    CheckSpec(
        "collector-ci",
        f"{_WF}collector-ci.yml",
        required_jobs=(
            "Lint",
            "Vulnerability Scan",
            "Unit Tests",
            "Spec Corpus Validation",
            "Build Artifacts",
            "Security Scan",
        ),
        events=("push",),
    ),
    CheckSpec(
        "pip-audit",
        f"{_WF}pip-audit.yml",
        required_jobs=("pip-audit (locked deps)",),
        events=_AUDIT_EVENTS,
        max_age_s=_DAY,
    ),
    CheckSpec(
        "npm-audit",
        f"{_WF}npm-audit.yml",
        required_jobs=(
            "npm-audit (ui/dashboard)",
            "npm-audit (packages/nova-dashboard)",
            "npm-audit (packages/nova-sdk-ts)",
        ),
        events=_AUDIT_EVENTS,
        max_age_s=_DAY,
    ),
    CheckSpec(
        "license-policy",
        f"{_WF}license-policy.yml",
        required_jobs=("license policy (ADR-0024)",),
        events=_AUDIT_EVENTS,
    ),
    CheckSpec(
        "metadata-store-security-gate",
        f"{_WF}metadata_store_security_gate.yml",
        required_jobs=("MetadataStore Security Gate",),
    ),
    # Not implemented yet: each is owned by its own issue and can never pass until it lands.
    CheckSpec(
        "golden-capsule",
        f"{_WF}ci.yml",
        required_jobs=("golden-capsule",),
        all_jobs_must_pass=False,
        on_failure="BLOCK",
        implemented=False,
        owner_issue="#225",
    ),
    CheckSpec(
        "oci-candidate-scan",
        f"{_WF}candidate-scan.yml",
        required_jobs=("candidate-scan",),
        on_failure="BLOCK",
        implemented=False,
        owner_issue="#218",
    ),
    CheckSpec(
        "helm-candidate-smoke",
        f"{_WF}helm-smoke.yml",
        required_jobs=("helm-smoke",),
        on_failure="BLOCK",
        implemented=False,
        owner_issue="#224",
    ),
    CheckSpec(
        "build-integrity",
        f"{_WF}build-integrity.yml",
        required_jobs=("build-integrity",),
        on_failure="BLOCK",
        implemented=False,
        owner_issue="#220",
    ),
    # Reported, never blocking, never a substitute for a mandatory check.
    CheckSpec("scorecard", f"{_WF}scorecard.yml", level="informational", events=_AUDIT_EVENTS),
    CheckSpec("release-toolchain", f"{_WF}release-toolchain.yml", level="informational"),
    CheckSpec("docs", f"{_WF}docs.yml", level="informational"),
    CheckSpec("sdk-ts", f"{_WF}sdk-ts.yml", level="informational"),
    CheckSpec("capture-action", f"{_WF}capture-action.yml", level="informational"),
)


def policy_digest(policy: tuple[CheckSpec, ...]) -> str:
    """Stable sha256 of a manifest: what an authorization binds to and a candidate cannot change."""
    canonical = json.dumps([asdict(spec) for spec in policy], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class ReleaseRequest:
    """Facts about the candidate, gathered by a trusted caller (never read from the candidate).

    ``tag_target_sha`` must be the PEELED commit the tag points at (``git rev-parse
    <tag>^{commit}``): an annotated tag, such as v0.109.0, has its own object id that is not
    the commit. ``main_vs_sha`` is GitHub's compare status for ``base=<sha>, head=main`` and
    ``promotion`` the recorded publication state: none | in_progress | published | partial.
    """

    repository: str
    event_name: str
    ref: str
    sha: str
    tag_target_sha: str | None
    main_vs_sha: str | None
    package_version: str | None
    policy_digest: str
    trusted_policy_digest: str
    authorization: dict[str, Any] | None
    promotion: str


@dataclass(frozen=True)
class Evidence:
    """Validated runs of one workflow for the candidate and the jobs of each run."""

    runs: tuple[dict[str, Any], ...]
    jobs: dict[int, tuple[dict[str, Any], ...]]


@dataclass(frozen=True)
class ReleaseVerdict:
    """``state`` is PASS, FAIL, PENDING, INVALID or BLOCK; only PASS authorizes a release."""

    state: str
    reasons: tuple[str, ...]
    notes: tuple[str, ...] = ()

    @property
    def authorizes(self) -> bool:
        return self.state == "PASS"


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise EvidenceError(f"timestamp is not a string: {value!r}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError(f"unparseable timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        raise EvidenceError(f"timestamp without a timezone: {value!r}")
    return parsed


def _listing(payload: Any, key: str) -> list[Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        raise EvidenceError(f"payload has no {key!r} list")
    items: list[Any] = payload[key]
    total = payload.get("total_count")
    if isinstance(total, int) and total > len(items):
        raise EvidenceError(f"truncated {key}: {len(items)} of {total} returned (pagination)")
    return items


def parse_runs(payload: Any) -> tuple[dict[str, Any], ...]:
    """Validate a ``/actions/workflows/<w>/runs?head_sha=`` payload; anything odd raises."""
    runs = []
    for raw in _listing(payload, "workflow_runs"):
        if not isinstance(raw, dict):
            raise EvidenceError("a workflow run is not an object")
        rid, repo = raw.get("id"), raw.get("head_repository")
        if not isinstance(rid, int) or isinstance(rid, bool):
            raise EvidenceError(f"run id is not an integer: {rid!r}")
        for field in ("status", "head_sha", "event", "path"):
            if not isinstance(raw.get(field), str):
                raise EvidenceError(f"run {rid}: {field} missing or not a string")
        if "conclusion" not in raw or not isinstance(raw["conclusion"], str | None):
            raise EvidenceError(f"run {rid}: conclusion missing or malformed")
        # GitHub sends null for a deleted fork and for a run with no branch. Such a run is
        # parsed, not fatal: whether it matters depends on its event (see _evaluate).
        if not isinstance(raw.get("head_branch"), str | None):
            raise EvidenceError(f"run {rid}: head_branch is not a string or null")
        if repo is not None and (
            not isinstance(repo, dict) or not isinstance(repo.get("full_name"), str)
        ):
            raise EvidenceError(f"run {rid}: head_repository.full_name malformed")
        runs.append({**raw, "created": _timestamp(raw.get("created_at"))})
    return tuple(runs)


def parse_jobs(payload: Any) -> tuple[dict[str, Any], ...]:
    """Validate a ``/actions/runs/<id>/jobs`` payload; a job without identity is an error."""
    jobs = []
    for raw in _listing(payload, "jobs"):
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not raw["name"]:
            raise EvidenceError("a job has no name")
        if not isinstance(raw.get("status"), str) or "conclusion" not in raw:
            raise EvidenceError(f"job {raw['name']!r}: status or conclusion missing")
        jobs.append(raw)
    return tuple(jobs)


def _identity_problems(req: ReleaseRequest) -> list[str]:
    """Reasons the request is not a release of the canonical repository (all -> INVALID)."""
    problems = []
    if req.repository != CANONICAL_REPO:
        problems.append(f"repository {req.repository!r} is not the canonical {CANONICAL_REPO!r}")
    if req.event_name not in ("push", "workflow_dispatch"):
        problems.append(f"event {req.event_name!r} cannot release")
    match = _TAG_REF.match(req.ref)
    if not match:
        problems.append(
            f"ref {req.ref!r} is not an immutable release tag refs/tags/vX.Y.Z "
            "(a dispatch from a branch or commit is not a release)"
        )
    if not _FULL_SHA.match(req.sha):
        problems.append(f"approved SHA {req.sha!r} is not a full lowercase 40-hex commit id")
    if req.tag_target_sha != req.sha:
        problems.append(
            f"the tag resolves to {req.tag_target_sha!r}, not the approved {req.sha!r} "
            "(compare the peeled commit, not an annotated tag object's own id)"
        )
    if req.main_vs_sha not in _MAIN_CONTAINS:
        problems.append(
            f"the release commit is not on {DEFAULT_BRANCH} ancestry (compare: {req.main_vs_sha!r})"
        )
    if match and req.package_version != match.group(1)[1:]:
        problems.append(
            f"package version {req.package_version!r} does not match tag {match.group(1)}"
        )
    if req.policy_digest != req.trusted_policy_digest:
        problems.append(
            "the evaluated policy is not the trusted policy (a candidate cannot supply its own)"
        )
    return problems


def _authorization_problem(req: ReleaseRequest) -> str | None:
    tag = _TAG_REF.match(req.ref)
    auth = req.authorization
    if not auth:
        return "no explicit owner authorization for this release"
    if not auth.get("approved_by"):
        return "authorization names no approver"
    if (
        tag is None
        or auth.get("tag") != tag.group(1)
        or auth.get("sha") != req.sha
        or auth.get("policy_digest") != req.policy_digest
    ):
        return "authorization does not match this tag, commit and policy version"
    return None


def _eligible(spec: CheckSpec, run: dict[str, Any], tag: str) -> bool:
    """Is this run acceptable release evidence for *spec* (event and ref)?"""
    if run["event"] not in spec.events:
        return False
    if run["event"] == "workflow_dispatch":
        return bool(run["head_branch"] == tag)  # a manual run counts only on the release tag
    return run["head_branch"] in (DEFAULT_BRANCH, tag)


def _how_to_satisfy(spec: CheckSpec, tag: str) -> str:
    """Deterministic instruction for a check that has no usable evidence yet."""
    options = []
    if "workflow_dispatch" in spec.events:
        options.append(f"dispatch {spec.workflow_path} on the release tag {tag}")
    others = [e for e in spec.events if e != "workflow_dispatch"]
    if others:
        options.append(f"a {'/'.join(others)} run of {spec.workflow_path} on exactly this SHA")
    text = "to satisfy it: " + " or ".join(options)
    if spec.max_age_s is not None:
        text += f"; evidence must be at most {spec.max_age_s / 3600:g}h old"
    return text


def _evaluate(
    spec: CheckSpec, ev: Evidence | EvidenceError | None, req: ReleaseRequest, now: datetime
) -> tuple[str, str]:
    """(state, reason) of one mandatory check; PENDING is turned into a failure by the caller."""
    fail = spec.on_failure
    if not spec.implemented:
        return "BLOCK", (
            f"{spec.key}: mandatory check is not implemented yet ({spec.owner_issue}); "
            "an unimplemented check never passes"
        )
    if isinstance(ev, EvidenceError):
        return fail, f"{spec.key}: evidence unavailable: {ev}"
    # Runs of an event this check never accepts (a fork's pull_request run, say) are neither
    # evidence nor a reason to distrust the evidence: anyone can open a pull request whose head
    # is the release commit. Identity is enforced only on runs that could count.
    runs = [r for r in (ev.runs if ev else ()) if r["event"] in spec.events]
    for r in runs:
        if r["head_sha"] != req.sha:
            return "INVALID", (
                f"{spec.key}: run {r['id']} is bound to a different SHA {r['head_sha'][:12]}, "
                f"not the approved {req.sha[:12]}"
            )
        repo = (r["head_repository"] or {}).get("full_name")
        if repo is None:
            return "INVALID", f"{spec.key}: run {r['id']} has an unknown head repository"
        if repo != CANONICAL_REPO:
            return "INVALID", f"{spec.key}: run {r['id']} comes from {repo}"
        if r["path"] != spec.workflow_path:
            return (
                "INVALID",
                f"{spec.key}: run {r['id']} is from {r['path']}, expected {spec.workflow_path}",
            )
    tag = _TAG_REF.match(req.ref)
    tag_name = tag.group(1) if tag else ""
    eligible = [r for r in runs if _eligible(spec, r, tag_name)]
    fresh = [
        r
        for r in eligible
        if spec.max_age_s is None or (now - r["created"]).total_seconds() <= spec.max_age_s
    ]
    if not fresh:
        why = "only stale evidence" if eligible else "no eligible run"
        return "PENDING", f"{spec.key}: {why} for {req.sha[:12]}; {_how_to_satisfy(spec, tag_name)}"
    latest = max(fresh, key=lambda r: (r["created"], r["id"]))  # the newest attempt decides
    url = latest.get("html_url", f"run {latest['id']}")
    jobs = (ev.jobs.get(latest["id"]) if ev else None) or None
    failed = [j["name"] for j in jobs or () if j["conclusion"] != "success"]
    if latest["status"] != "completed":
        return "PENDING", f"{spec.key}: run {latest['id']} is {latest['status']}"
    if latest["conclusion"] != "success":
        detail = f"; failing jobs: {', '.join(failed)}" if failed else ""
        return (
            fail,
            f"{spec.key}: latest run {latest['id']} concluded "
            f"{latest['conclusion']!r}{detail} ({url})",
        )
    if jobs is None:
        return fail, f"{spec.key}: jobs of run {latest['id']} were not retrieved"
    by_name = {j["name"]: j for j in jobs}
    problems = [f"required job {n!r} is missing" for n in spec.required_jobs if n not in by_name]
    for name, j in by_name.items():
        if (name in spec.required_jobs or spec.all_jobs_must_pass) and j["conclusion"] != "success":
            problems.append(f"job {name!r} concluded {j['conclusion']!r}")
    if problems:
        return fail, f"{spec.key}: run {latest['id']}: " + "; ".join(problems)
    return "PASS", f"{spec.key}: run {latest['id']} passed"


def decide_release(
    policy: tuple[CheckSpec, ...],
    req: ReleaseRequest,
    evidence: Mapping[str, Evidence | EvidenceError],
    *,
    now: datetime,
    waited: float,
    timeout: float,
) -> ReleaseVerdict:
    """The single release verdict. Severity: INVALID > BLOCK > FAIL > PENDING > PASS.

    PENDING (evidence still running or absent) becomes the check's failure state once
    ``waited >= timeout``. Only PASS authorizes a release.
    """
    problems = _identity_problems(req)
    if req.policy_digest != policy_digest(policy):
        problems.append("the policy under evaluation is not the one the request declares")
    if problems:
        return ReleaseVerdict("INVALID", tuple(problems))

    outcomes: list[tuple[str, str]] = []
    notes: list[str] = []
    for spec in policy:
        state, reason = _evaluate(spec, evidence.get(spec.key), req, now)
        if spec.level != "mandatory":
            notes.append(f"informational {reason} [{state}]")
            continue
        if state == "PENDING" and waited >= timeout:
            state, reason = spec.on_failure, f"{reason} (timed out waiting)"
        outcomes.append((state, reason))
    auth = _authorization_problem(req)
    if auth:
        outcomes.append(("BLOCK", auth))
    if req.promotion != _PROMOTION_OK:
        advice = (
            "reconcile the partial release by hand; it is not silently rolled back or re-published"
            if req.promotion == "partial"
            else "this release is already published or being published; no second promotion"
        )
        outcomes.append(
            ("BLOCK", f"promotion state is {req.promotion.replace('_', ' ')}: {advice}")
        )

    worst = max((s for s, _ in outcomes), key=_SEVERITY.__getitem__, default="PASS")
    if worst == "PASS":
        return ReleaseVerdict(
            "PASS", (f"{len(outcomes)} mandatory checks passed on {req.sha}",), tuple(notes)
        )
    return ReleaseVerdict(worst, tuple(r for s, r in outcomes if s != "PASS"), tuple(notes))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail unless CI passed on this commit.")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--workflow", default="ci.yml")
    parser.add_argument("--timeout", type=float, default=100 * 60)
    parser.add_argument("--grace", type=float, default=5 * 60)
    parser.add_argument("--interval", type=float, default=30)
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN", "")
    if not (args.repo and args.sha and token):
        print("require-green-ci: need --repo, --sha and GITHUB_TOKEN", file=sys.stderr)
        return 2

    result = wait_for_green(
        _github_fetch(token),
        args.repo,
        args.sha,
        workflow=args.workflow,
        timeout=args.timeout,
        grace=args.grace,
        interval=args.interval,
    )
    if result.state == "pass":
        print(f"require-green-ci: {result.message}")
        return 0
    print(f"::error title=Release blocked::{result.message}".replace("\n", "%0A"))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
