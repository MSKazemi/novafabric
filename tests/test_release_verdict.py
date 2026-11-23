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

"""Offline negative tests for the proposed release verdict (design, not activated).

The decision is a pure function over fake GitHub API payloads: no network, no publish.
Its anchor case is the real v0.109.0 release, where ``CI`` passed on the release commit,
``Collector CI`` failed there (its ``govulncheck`` job), and the four publishing
workflows still ran because the gate looked at ``ci.yml`` only. The run records below are
copied from the GitHub API (run ids, conclusions, SHA, timestamps).
"""

from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "require_green_ci_verdict", REPO / "scripts" / "require_green_ci.py"
)
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
sys.modules["require_green_ci_verdict"] = gate
_spec.loader.exec_module(gate)

CANON = "MSKazemi/novafabric"
SHA = "9b7b31b9d54af55308f9c6b5b3b07304d8a9e5ed"  # the real v0.109.0 release commit
OTHER_SHA = "f08ed3ce34b6e3e03481e40a7747d4bc16f64a45"
TAG = "v0.109.0"
NOW = datetime(2026, 11, 22, 17, 0, 0, tzinfo=UTC)
TIMEOUT = 6000.0

# --- real run records (GitHub API, 2026-11-22) ---------------------------------------
REAL = {
    # key: (run id, workflow path, head_branch, conclusion, created_at)
    "ci": (45044063266, ".github/workflows/ci.yml", "main", "success", "2026-11-22T16:28:52Z"),
    "collector-ci": (
        45046197086,
        ".github/workflows/collector-ci.yml",
        TAG,
        "failure",
        "2026-11-22T16:51:59Z",
    ),
    "pip-audit": (
        45044063380,
        ".github/workflows/pip-audit.yml",
        "main",
        "success",
        "2026-11-22T16:28:52Z",
    ),
    "license-policy": (
        45044063291,
        ".github/workflows/license-policy.yml",
        "main",
        "success",
        "2026-11-22T16:28:52Z",
    ),
    "metadata-store-security-gate": (
        45044063259,
        ".github/workflows/metadata_store_security_gate.yml",
        "main",
        "success",
        "2026-11-22T16:28:52Z",
    ),
}
REAL_JOBS = {
    "ci": [
        ("seal-latency-gate", "success"),
        ("lint", "success"),
        *[(f"unit ({i}/6)", "success") for i in range(1, 7)],
        ("typecheck", "success"),
        ("capture-overhead-gate", "success"),
        ("web", "success"),
        ("integration", "success"),
        ("coverage", "success"),
    ],
    "collector-ci": [
        ("Spec Corpus Validation", "success"),
        ("Lint", "success"),
        ("Unit Tests", "success"),
        ("Vulnerability Scan", "failure"),
        ("Build Artifacts", "success"),
        ("Security Scan", "success"),
    ],
    "pip-audit": [("pip-audit (locked deps)", "success")],
    "license-policy": [("license policy (ADR-0024)", "success")],
    "metadata-store-security-gate": [("MetadataStore Security Gate", "success")],
}


def run(
    rid: int,
    path: str,
    *,
    branch: str = "main",
    event: str = "push",
    conclusion: str | None = "success",
    status: str = "completed",
    created: str = "2026-11-22T16:28:52Z",
    sha: str = SHA,
    repo: str = CANON,
    attempt: int = 1,
) -> dict[str, Any]:
    return {
        "id": rid,
        "status": status,
        "conclusion": conclusion,
        "created_at": created,
        "run_attempt": attempt,
        "head_sha": sha,
        "head_branch": branch,
        "event": event,
        "path": path,
        "head_repository": {"full_name": repo},
        "html_url": f"https://github.com/{CANON}/actions/runs/{rid}",
    }


def job(name: str, conclusion: str | None = "success", status: str = "completed") -> dict[str, Any]:
    return {"name": name, "status": status, "conclusion": conclusion}


def evidence(runs: list[dict[str, Any]], jobs: dict[int, list[dict[str, Any]]]) -> Any:
    return gate.Evidence(
        runs=gate.parse_runs({"total_count": len(runs), "workflow_runs": runs}),
        jobs={rid: gate.parse_jobs({"total_count": len(j), "jobs": j}) for rid, j in jobs.items()},
    )


def request(policy: Any = None, **over: Any) -> Any:
    """A valid release request bound to *policy* (default: the all-implemented test policy)."""
    digest = gate.policy_digest(POLICY if policy is None else policy)
    base: dict[str, Any] = {
        "repository": CANON,
        "event_name": "push",
        "ref": f"refs/tags/{TAG}",
        "sha": SHA,
        "tag_target_sha": SHA,
        "main_vs_sha": "ahead",  # compare(base=sha, head=main): main contains the release commit
        "package_version": "0.109.0",
        "policy_digest": digest,
        "trusted_policy_digest": digest,
        "authorization": {"tag": TAG, "sha": SHA, "policy_digest": digest, "approved_by": "owner"},
        "promotion": "none",
    }
    base.update(over)
    return gate.ReleaseRequest(**base)


def implemented(policy: Any) -> tuple[Any, ...]:
    """The design manifest with every placeholder check treated as implemented."""
    return tuple(dataclasses.replace(s, implemented=True) for s in policy)


def world(policy: Any, **per_key: Any) -> dict[str, Any]:
    """All-green evidence for every mandatory and informational check of *policy*."""
    out: dict[str, Any] = {}
    rid = 1000
    for spec in policy:
        rid += 1
        job_names = list(spec.required_jobs) or [spec.key]
        branch = "main"
        r = run(rid, spec.workflow_path, branch=branch)
        out[spec.key] = evidence([r], {rid: [job(n) for n in job_names]})
    out.update(per_key)
    return out


POLICY = implemented(gate.RELEASE_POLICY_V1)


def decide(
    req: Any = None,
    ev: Any = None,
    *,
    policy: Any = None,
    waited: float = 0.0,
    now: datetime = NOW,
) -> Any:
    pol = POLICY if policy is None else policy
    return gate.decide_release(
        pol,
        req if req is not None else request(pol),
        ev if ev is not None else world(pol),
        now=now,
        waited=waited,
        timeout=TIMEOUT,
    )


def spec_of(key: str, policy: Any = POLICY) -> Any:
    return next(s for s in policy if s.key == key)


def replace_run(key: str, policy: Any = POLICY, **over: Any) -> Any:
    """Evidence for *key* with the all-green run replaced by overrides."""
    spec = spec_of(key, policy)
    job_names = list(spec.required_jobs) or [spec.key]
    r = run(2000, spec.workflow_path, **over)
    return evidence([r], {2000: [job(n) for n in job_names]})


# ============================ 1. positive ===========================================


def test_01_all_mandatory_checks_green_on_the_exact_sha_pass() -> None:
    v = decide()
    assert v.state == "PASS", v.reasons
    assert v.authorizes is True


def test_01b_the_design_manifest_itself_can_never_pass_while_checks_are_unimplemented() -> None:
    """#218, #224, #225 and build-integrity do not exist yet: unimplemented never means passing."""
    v = decide(policy=gate.RELEASE_POLICY_V1)
    assert v.state == "BLOCK"
    text = " ".join(v.reasons)
    for issue in ("#218", "#224", "#225", "#220"):
        assert issue in text
    assert v.authorizes is False


# ============================ 2. the historical incident ============================


def _historical_runs() -> dict[str, Any]:
    ev: dict[str, Any] = {}
    for key, (rid, path, branch, conclusion, created) in REAL.items():
        r = run(rid, path, branch=branch, conclusion=conclusion, created=created)
        ev[key] = evidence([r], {rid: [job(n, c) for n, c in REAL_JOBS[key]]})
    return ev  # no npm-audit run existed on this SHA (path-filtered); none is fabricated


def test_02_the_current_ci_only_decision_accepts_the_real_v0_109_0_commit() -> None:
    ci_only = [
        {
            "id": 45044063266,
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-11-22T16:28:52Z",
            "html_url": f"https://github.com/{CANON}/actions/runs/45044063266",
        }
    ]
    assert gate.verdict(ci_only, waited=0, grace=300).state == "pass"  # the real gap


HIST = tuple(s for s in gate.RELEASE_POLICY_V1 if s.implemented)  # the checks that exist today


def test_02b_the_proposed_decision_rejects_the_same_commit_naming_the_failed_job() -> None:
    v = decide(request(HIST), _historical_runs(), policy=HIST, waited=0)
    assert v.state == "FAIL"
    assert any("collector-ci" in r and "Vulnerability Scan" in r for r in v.reasons)
    assert any("45046197086" in r for r in v.reasons)  # cites the evidence run
    assert v.authorizes is False


def test_02c_collector_failure_is_not_forgiven_by_the_other_green_checks() -> None:
    assert decide(request(HIST), _historical_runs(), policy=HIST).state != "PASS"


def test_02d_even_with_collector_green_the_real_commit_lacked_npm_audit_evidence() -> None:
    """npm-audit is path-filtered and never ran on the release SHA: a second, honest gap."""
    ev = _historical_runs()
    rid, path, branch, _, created = REAL["collector-ci"]
    ev["collector-ci"] = evidence(
        [run(rid, path, branch=branch, created=created)],
        {rid: [job(n) for n, _ in REAL_JOBS["collector-ci"]]},
    )
    assert decide(request(HIST), ev, policy=HIST, waited=0).state == "PENDING"
    final = decide(request(HIST), ev, policy=HIST, waited=TIMEOUT)
    assert final.state == "FAIL" and any("npm-audit" in r for r in final.reasons)


# ============================ 3-4. audit checks =====================================


def test_03_a_failing_pip_audit_blocks_the_release() -> None:
    ev = world(POLICY, **{"pip-audit": replace_run("pip-audit", conclusion="failure")})
    v = decide(ev=ev)
    assert v.state == "FAIL"
    assert any("pip-audit" in r for r in v.reasons)


def test_04_an_expired_npm_waiver_fails_the_release() -> None:
    spec = spec_of("npm-audit")
    jobs = [job(n) for n in spec.required_jobs[:-1]] + [job(spec.required_jobs[-1], "failure")]
    r = run(2001, spec.workflow_path, conclusion="failure")
    ev = world(POLICY, **{"npm-audit": evidence([r], {2001: jobs})})
    v = decide(ev=ev)
    assert v.state == "FAIL"
    assert any("npm-audit" in x for x in v.reasons)


def test_04b_the_verdict_has_no_waiver_input_a_waiver_lives_inside_the_check() -> None:
    params = inspect.signature(gate.decide_release).parameters
    assert not [p for p in params if "waive" in p.lower() or "ignore" in p.lower()]
    assert not [f.name for f in dataclasses.fields(gate.CheckSpec) if "waive" in f.name.lower()]


@pytest.mark.parametrize(
    "conclusion", ["cancelled", "timed_out", "skipped", "neutral", "action_required", "stale", None]
)
def test_04c_every_completed_conclusion_but_success_fails_a_mandatory_check(
    conclusion: str | None,
) -> None:
    ev = world(POLICY, **{"collector-ci": replace_run("collector-ci", conclusion=conclusion)})
    v = decide(ev=ev)
    assert v.state == "FAIL" and any("collector-ci" in r for r in v.reasons)


# ============================ 5-9. evidence selection ===============================


def test_05_a_missing_required_run_is_pending_then_a_blocking_failure() -> None:
    ev = world(POLICY)
    del ev["npm-audit"]
    early = decide(ev=ev, waited=10)
    assert early.state == "PENDING" and early.authorizes is False
    late = decide(ev=ev, waited=TIMEOUT)
    assert late.state == "FAIL"
    assert any("npm-audit" in r and "timed out" in r for r in late.reasons)


def test_05b_an_unfinished_run_is_pending_then_failure_at_the_timeout() -> None:
    running = replace_run("ci", status="in_progress", conclusion=None)
    ev = world(POLICY, ci=running)
    assert decide(ev=ev, waited=1).state == "PENDING"
    assert decide(ev=ev, waited=TIMEOUT).state == "FAIL"


def test_06_a_green_run_from_a_different_sha_is_invalid_evidence() -> None:
    ev = world(POLICY, ci=replace_run("ci", sha=OTHER_SHA))
    v = decide(ev=ev)
    assert v.state == "INVALID"
    assert any("different SHA" in r or "another SHA" in r for r in v.reasons)


def test_07_an_earlier_success_followed_by_a_failed_latest_attempt_fails() -> None:
    spec = spec_of("ci")
    names = list(spec.required_jobs)
    old = run(1, spec.workflow_path, created="2026-11-22T16:00:00Z")
    new = run(2, spec.workflow_path, created="2026-11-22T16:30:00Z", conclusion="failure")
    ev = world(
        POLICY,
        ci=evidence([old, new], {1: [job(n) for n in names], 2: [job(n) for n in names]}),
    )
    assert decide(ev=ev).state == "FAIL"


def test_08_a_same_sha_successful_rerun_after_a_failed_attempt_is_eligible() -> None:
    spec = spec_of("ci")
    names = list(spec.required_jobs)
    failed = run(1, spec.workflow_path, created="2026-11-22T16:00:00Z", conclusion="failure")
    rerun = run(2, spec.workflow_path, created="2026-11-22T16:30:00Z", attempt=2)
    ev = world(
        POLICY,
        ci=evidence(
            [failed, rerun], {1: [job(n, "failure") for n in names], 2: [job(n) for n in names]}
        ),
    )
    v = decide(ev=ev)
    assert v.state == "PASS", v.reasons


def test_09_a_required_job_skipped_unexpectedly_fails() -> None:
    spec = spec_of("ci")
    names = list(spec.required_jobs)
    jobs = [job(n, "skipped" if n == "integration" else "success") for n in names]
    r = run(2000, spec.workflow_path)
    ev = world(POLICY, ci=evidence([r], {2000: jobs}))
    v = decide(ev=ev)
    assert v.state == "FAIL"
    assert any("integration" in x and "skipped" in x for x in v.reasons)


def test_09b_a_green_run_that_lacks_a_required_job_fails() -> None:
    spec = spec_of("ci")
    names = [n for n in spec.required_jobs if n != "coverage"]
    r = run(2000, spec.workflow_path)
    ev = world(POLICY, ci=evidence([r], {2000: [job(n) for n in names]}))
    v = decide(ev=ev)
    assert v.state == "FAIL" and any("coverage" in x for x in v.reasons)


def test_09c_an_extra_unexpected_failed_job_in_a_green_looking_run_fails() -> None:
    spec = spec_of("ci")
    names = list(spec.required_jobs)
    r = run(2000, spec.workflow_path)
    jobs = [job(n) for n in names] + [job("surprise", "failure")]
    ev = world(POLICY, ci=evidence([r], {2000: jobs}))
    assert decide(ev=ev).state == "FAIL"


# ============================ 10-12. release identity ===============================


def test_10_a_dispatch_from_an_arbitrary_branch_is_invalid() -> None:
    v = decide(request(event_name="workflow_dispatch", ref="refs/heads/feature/x"))
    assert v.state == "INVALID"
    assert any("tag" in r for r in v.reasons)


def test_10b_a_dispatch_from_the_trusted_tag_is_not_rejected_for_being_a_dispatch() -> None:
    v = decide(request(event_name="workflow_dispatch"))
    assert v.state == "PASS", v.reasons


def test_10c_a_release_cannot_be_driven_by_a_schedule_event() -> None:
    assert decide(request(event_name="schedule")).state == "INVALID"


@pytest.mark.parametrize("status", ["diverged", "behind", None, "weird"])
def test_11_a_tag_not_on_trusted_main_ancestry_is_invalid(status: str | None) -> None:
    v = decide(request(main_vs_sha=status))
    assert v.state == "INVALID"
    assert any("main" in r for r in v.reasons)


def test_11b_main_itself_as_the_release_commit_is_accepted() -> None:
    assert decide(request(main_vs_sha="identical")).state == "PASS"


def test_11c_a_tag_that_resolves_to_a_different_commit_is_invalid() -> None:
    assert decide(request(tag_target_sha=OTHER_SHA)).state == "INVALID"
    assert decide(request(tag_target_sha=None)).state == "INVALID"


def test_11d_package_version_must_match_the_tag() -> None:
    assert decide(request(package_version="0.108.0")).state == "INVALID"
    assert decide(request(package_version=None)).state == "INVALID"


@pytest.mark.parametrize(
    "ref", ["refs/tags/latest", "refs/tags/v1", "refs/tags/v0.109", "v0.109.0"]
)
def test_11e_only_a_semver_release_tag_ref_is_a_release(ref: str) -> None:
    assert decide(request(ref=ref)).state == "INVALID"


@pytest.mark.parametrize("sha", ["", "9b7b31b", "Z" * 40, SHA.upper()])
def test_11f_the_approved_sha_must_be_a_full_lowercase_sha(sha: str) -> None:
    assert decide(request(sha=sha, tag_target_sha=sha)).state == "INVALID"


def test_12_the_wrong_source_repository_is_invalid() -> None:
    assert decide(request(repository="someone-else/novafabric")).state == "INVALID"


def test_12b_evidence_from_a_fork_repository_is_invalid() -> None:
    ev = world(POLICY, ci=replace_run("ci", repo="attacker/novafabric"))
    assert decide(ev=ev).state == "INVALID"


def test_12c_a_run_from_an_unexpected_workflow_file_is_invalid_evidence() -> None:
    ev = world(
        POLICY, ci=evidence([run(2000, ".github/workflows/evil.yml")], {2000: [job("lint")]})
    )
    assert decide(ev=ev).state == "INVALID"


def test_12d_a_candidate_cannot_supply_its_own_policy() -> None:
    weaker = tuple(s for s in POLICY if s.key != "collector-ci")
    req = request(weaker, trusted_policy_digest=gate.policy_digest(POLICY))
    v = decide(req, world(weaker), policy=weaker)
    assert v.state == "INVALID"
    assert any("policy" in r for r in v.reasons)


def test_12e_the_policy_actually_evaluated_must_be_the_one_the_request_declares() -> None:
    """Declared and trusted digests agree, but a different manifest is passed in: INVALID."""
    weaker = tuple(s for s in POLICY if s.key != "collector-ci")
    v = decide(request(POLICY), world(weaker), policy=weaker)
    assert v.state == "INVALID"
    assert any("not the one the request declares" in r for r in v.reasons)


def test_12f_a_malformed_sha_is_rejected_by_identity_alone() -> None:
    """No evidence at all, so only the identity rule can produce INVALID here."""
    for bad in ("", "9b7b31b", "Z" * 40, SHA.upper(), SHA + "0"):
        v = decide(request(sha=bad, tag_target_sha=bad), {})
        assert v.state == "INVALID", bad


# ============================ 13. authorization =====================================


def test_13_missing_release_authorization_blocks() -> None:
    v = decide(request(authorization=None))
    assert v.state == "BLOCK"
    assert any("authorization" in r for r in v.reasons)


@pytest.mark.parametrize(
    "auth",
    [
        {"tag": "v0.108.0", "sha": SHA, "approved_by": "owner"},
        {"tag": TAG, "sha": OTHER_SHA, "approved_by": "owner"},
        {"tag": TAG, "sha": SHA, "approved_by": ""},
        {"tag": TAG, "sha": SHA},
    ],
)
def test_13b_an_authorization_for_something_else_blocks(auth: dict[str, Any]) -> None:
    auth = {"policy_digest": gate.policy_digest(POLICY), **auth}
    assert decide(request(authorization=auth)).state == "BLOCK"


def test_13c_an_authorization_for_another_policy_version_blocks() -> None:
    auth = {"tag": TAG, "sha": SHA, "approved_by": "owner", "policy_digest": "0" * 64}
    assert decide(request(authorization=auth)).state == "BLOCK"


# ============================ 14-16. candidate-artifact gates =======================


def test_14_a_failed_oci_candidate_scan_blocks() -> None:
    ev = world(
        POLICY, **{"oci-candidate-scan": replace_run("oci-candidate-scan", conclusion="failure")}
    )
    v = decide(ev=ev)
    assert v.state == "BLOCK" and any("oci-candidate-scan" in r for r in v.reasons)


def test_15_a_failed_helm_validation_blocks() -> None:
    ev = world(
        POLICY,
        **{"helm-candidate-smoke": replace_run("helm-candidate-smoke", conclusion="failure")},
    )
    assert decide(ev=ev).state == "BLOCK"


def test_16_missing_golden_capsule_evidence_blocks_once_the_wait_is_over() -> None:
    ev = world(POLICY)
    del ev["golden-capsule"]
    assert decide(ev=ev, waited=1).state == "PENDING"
    final = decide(ev=ev, waited=TIMEOUT)
    assert final.state == "BLOCK"
    assert any("golden-capsule" in r for r in final.reasons)


def test_16b_a_green_ci_run_without_the_golden_job_does_not_satisfy_golden() -> None:
    """The golden check is the named job: a CI run that merely lacks it is not evidence."""
    spec = spec_of("golden-capsule")
    r = run(2002, spec.workflow_path)
    ev = world(POLICY, **{"golden-capsule": evidence([r], {2002: [job("lint")]})})
    assert decide(ev=ev, waited=TIMEOUT).state in {"BLOCK", "FAIL"}


# ============================ 17. evidence acquisition failures ======================


def test_17_an_api_timeout_fails_closed() -> None:
    ev = world(POLICY, ci=gate.EvidenceError("timeout fetching workflow runs"))
    v = decide(ev=ev)
    assert v.state == "FAIL" and any("timeout" in r for r in v.reasons)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"workflow_runs": "nope"},
        {"workflow_runs": [{"id": 1}]},
        {"workflow_runs": [run(1, "p") | {"id": "1"}]},
        {"workflow_runs": [run(1, "p") | {"created_at": "yesterday"}]},
        {"workflow_runs": [run(1, "p") | {"head_repository": None}]},
        {"total_count": 3, "workflow_runs": [run(1, "p")]},  # pagination: 2 runs missing
    ],
)
def test_17b_malformed_or_truncated_run_payloads_are_rejected(payload: Any) -> None:
    with pytest.raises(gate.EvidenceError):
        gate.parse_runs(payload)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"jobs": [{"conclusion": "success", "status": "completed"}]},  # no job identity
        {"jobs": [{"name": "", "conclusion": "success", "status": "completed"}]},
        {"jobs": [{"name": "x", "status": "completed"}]},
        {"total_count": 2, "jobs": [job("x")]},  # pagination
    ],
)
def test_17c_malformed_job_payloads_are_rejected(payload: Any) -> None:
    with pytest.raises(gate.EvidenceError):
        gate.parse_jobs(payload)


def test_17d_a_run_whose_jobs_were_never_fetched_fails() -> None:
    spec = spec_of("ci")
    r = run(2000, spec.workflow_path)
    ev = world(POLICY, ci=evidence([r], {}))
    v = decide(ev=ev)
    assert v.state == "FAIL" and any("jobs" in x for x in v.reasons)


# ============================ 18. duplicate / partial promotion ======================


@pytest.mark.parametrize("promotion", ["in_progress", "published", "partial"])
def test_18_a_concurrent_duplicate_or_partial_promotion_blocks(promotion: str) -> None:
    v = decide(request(promotion=promotion))
    assert v.state == "BLOCK"
    assert any(promotion.replace("_", " ") in r or "promotion" in r for r in v.reasons)


def test_18b_a_partial_release_asks_for_reconciliation_not_a_rollback() -> None:
    v = decide(request(promotion="partial"))
    assert any("reconcil" in r for r in v.reasons)
    assert not any("rollback" in r.lower() and "automatic" in r.lower() for r in v.reasons)


# ============================ staleness, informational, precedence ===================


def test_19_old_audit_evidence_on_the_same_sha_is_stale_not_proof() -> None:
    old = (NOW - timedelta(days=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ev = world(POLICY, **{"pip-audit": replace_run("pip-audit", created=old)})
    assert decide(ev=ev, waited=1).state == "PENDING"
    final = decide(ev=ev, waited=TIMEOUT)
    assert final.state == "FAIL" and any("stale" in r for r in final.reasons)


def test_19b_a_scheduled_audit_run_is_not_release_evidence_unless_listed_and_fresh() -> None:
    ev = world(POLICY, ci=replace_run("ci", event="schedule"))
    assert decide(ev=ev, waited=TIMEOUT).state == "FAIL"  # ci.yml has no schedule: ignored


def test_19c_dispatch_evidence_from_a_non_release_branch_is_ignored() -> None:
    ev = world(POLICY, ci=replace_run("ci", event="workflow_dispatch", branch="feature/x"))
    assert decide(ev=ev, waited=TIMEOUT).state == "FAIL"


@pytest.mark.parametrize(
    "branch",
    ["feature/x", "release/0.109", "master", "refs/heads/main", "dependabot/uv/x", "v0.108.0"],
)
def test_19d_push_evidence_from_a_branch_other_than_main_or_the_release_tag_is_not_evidence(
    branch: str,
) -> None:
    """Same SHA, canonical repo, right workflow, all green: still not release evidence."""
    ev = world(POLICY, ci=replace_run("ci", branch=branch))
    assert decide(ev=ev, waited=1).state == "PENDING"
    final = decide(ev=ev, waited=TIMEOUT)
    assert final.state == "FAIL"
    assert any(r.startswith("ci:") and "no eligible run" in r for r in final.reasons)


def test_19e_a_scheduled_run_on_another_branch_is_not_evidence_either() -> None:
    ev = world(
        POLICY, **{"pip-audit": replace_run("pip-audit", event="schedule", branch="feature/x")}
    )
    assert decide(ev=ev, waited=TIMEOUT).state == "FAIL"


@pytest.mark.parametrize("branch", ["main", TAG])
def test_19f_the_same_push_run_on_main_or_the_release_tag_does_count(branch: str) -> None:
    ev = world(POLICY, ci=replace_run("ci", branch=branch))
    v = decide(ev=ev)
    assert v.state == "PASS", v.reasons


def test_20_an_informational_failure_never_blocks_and_never_substitutes() -> None:
    info = next(s for s in POLICY if s.level == "informational")
    ev = world(POLICY, **{info.key: replace_run(info.key, conclusion="failure")})
    assert decide(ev=ev).state == "PASS"
    ev = world(POLICY)
    del ev["ci"]
    assert decide(ev=ev, waited=TIMEOUT).state == "FAIL"  # green informational cannot cover it


def test_21_severity_order_is_invalid_then_block_then_fail_then_pending() -> None:
    bad_ci = replace_run("ci", conclusion="failure")
    both = decide(request(promotion="partial"), world(POLICY, ci=bad_ci))
    assert both.state == "BLOCK"
    assert decide(request(repository="x/y"), world(POLICY, ci=bad_ci)).state == "INVALID"
    ev = world(POLICY, ci=bad_ci)
    del ev["npm-audit"]
    assert decide(ev=ev, waited=1).state == "FAIL"  # fail beats pending


def test_22_only_pass_authorizes() -> None:
    for state in ("FAIL", "PENDING", "INVALID", "BLOCK"):
        assert gate.ReleaseVerdict(state, ()).authorizes is False
    assert gate.ReleaseVerdict("PASS", ()).authorizes is True


def test_23_policy_digest_is_stable_and_changes_with_any_mandatory_field() -> None:
    d = gate.policy_digest(POLICY)
    assert d == gate.policy_digest(tuple(POLICY))
    assert len(d) == 64
    changed = tuple(
        dataclasses.replace(s, level="informational") if s.key == "ci" else s for s in POLICY
    )
    assert gate.policy_digest(changed) != d


def test_24_the_manifest_classifies_every_check_and_names_owning_issues() -> None:
    levels = {s.key: s.level for s in gate.RELEASE_POLICY_V1}
    for key in (
        "ci",
        "collector-ci",
        "pip-audit",
        "npm-audit",
        "license-policy",
        "metadata-store-security-gate",
    ):
        assert levels[key] == "mandatory"
    unimplemented = {s.key: s for s in gate.RELEASE_POLICY_V1 if not s.implemented}
    assert set(unimplemented) == {
        "golden-capsule",
        "oci-candidate-scan",
        "helm-candidate-smoke",
        "build-integrity",
    }
    assert all(s.level == "mandatory" and s.owner_issue for s in unimplemented.values())
    assert all(s.on_failure == "BLOCK" for s in unimplemented.values())


# ============================ the design stays disconnected ==========================


def test_25_the_existing_gate_surface_is_unchanged() -> None:
    for name in ("verdict", "wait_for_green", "main", "Verdict", "_github_fetch"):
        assert hasattr(gate, name)
    assert gate.verdict([], waited=0, grace=10).state == "wait"


def test_26_no_workflow_calls_the_proposed_verdict_yet() -> None:
    for wf in sorted((REPO / ".github" / "workflows").glob("*.yml")):
        text = wf.read_text()
        assert "decide_release" not in text, wf.name
        assert "RELEASE_POLICY" not in text, wf.name


# ============================ manifest vs the real workflow files ====================


def _job_names(workflow_path: str) -> set[str]:
    """Display names GitHub gives the jobs of a workflow, expanding single-axis matrices."""
    import yaml

    doc = yaml.safe_load((REPO / workflow_path).read_text())
    names: set[str] = set()
    for key, spec in doc["jobs"].items():
        template = str(spec.get("name", key))
        matrix = (spec.get("strategy") or {}).get("matrix") or {}
        axes = {k: v for k, v in matrix.items() if isinstance(v, list)}
        if not axes:
            names.add(template)
            continue
        (axis, values), *_ = axes.items()
        for value in values:
            names.add(
                template.replace(f"${{{{ matrix.{axis} }}}}", str(value)).replace(
                    "${{ strategy.job-total }}", str(len(values))
                )
            )
    return names


@pytest.mark.parametrize(
    "spec", [s for s in gate.RELEASE_POLICY_V1 if s.implemented], ids=lambda s: s.key
)
def test_27_every_required_job_of_an_implemented_check_exists_in_its_workflow(spec: Any) -> None:
    """If a workflow renames or drops a job the manifest names, this fails instead of the gate."""
    assert (REPO / spec.workflow_path).is_file(), spec.workflow_path
    missing = set(spec.required_jobs) - _job_names(spec.workflow_path)
    assert not missing, f"{spec.key}: {sorted(missing)} not produced by {spec.workflow_path}"
