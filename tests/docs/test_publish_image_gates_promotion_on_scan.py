# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""No production image tag, signature or attestation is reachable before the scan passes.

Issue #218. ``publish-image.yml`` used to push the multi-arch image to GHCR (and Docker
Hub) with ``docker/build-push-action`` and only *then* run trivy on the pushed tag. A
failing scan therefore failed a job whose image was already public, under its release
tags. The gate also scanned a single platform: trivy scans the runner's platform
(linux/amd64) unless told otherwise, so the arm64 half of the manifest list was never
looked at.

The invariants, asserted on the parsed workflow rather than on prose:

* the image is built ONCE, into a candidate registry that is not a production registry;
* a trivy gate covers the exact digest and EVERY platform the build produces;
* every step that can write to a production registry (login, copy, sign, attest, tag)
  comes after the last gate step, and none of them can run when an earlier step failed;
* production tags move last: after the digest was copied, verified, signed and attested;
* a manual dispatch runs the same job - there is no input, condition or second path
  that skips the scan.

What counts as "writes to production" is derived from what a step does (a login, a
promote/tag command, ``cosign sign``, an attestation action, a build that pushes to a
non-candidate registry), never from a hand-kept list of step names - a list of names is
what a renamed step silently escapes.

Local composite actions (``uses: ./.github/actions/<name>``) are expanded in place, so
the gate may live in one and be shared with ``release-toolchain.yml``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-image.yml"
JOB = "image"

CANDIDATE_HOSTS = ("localhost:", "127.0.0.1:")
WRITE_COMMANDS = (
    "release_image.py promote",
    "release_image.py tag",
    "imagetools create",
    "cosign sign",
    "cosign attest",
    "docker push",
    "docker build",
    "docker buildx build",
    "buildctl",
    "helm push",
    "skopeo copy",
    "crane copy",
    "crane push",
)
WRITE_ACTIONS = ("docker/login-action", "actions/attest-build-provenance")


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _subst(text: str, env: dict[str, str], inputs: dict[str, str]) -> str:
    def repl(m: re.Match[str]) -> str:
        kind, name = m.group(1), m.group(2)
        return (env if kind == "env" else inputs).get(name, m.group(0))

    return re.sub(r"\$\{\{\s*(env|inputs)\.([A-Za-z0-9_-]+)\s*\}\}", repl, text)


def _steps() -> list[dict[str, Any]]:
    """The job's steps with local composite actions expanded in place."""
    wf = _load(WORKFLOW)
    job = wf["jobs"][JOB]
    env = {**(wf.get("env") or {}), **(job.get("env") or {})}
    out: list[dict[str, Any]] = []
    for step in job.get("steps") or []:
        uses = str(step.get("uses") or "")
        if uses.startswith("./.github/actions/"):
            composite = _load(ROOT / uses.removeprefix("./") / "action.yml")
            inputs = {k: _subst(str(v), env, {}) for k, v in (step.get("with") or {}).items()}
            for inner in composite["runs"]["steps"]:
                inner = dict(inner)
                inner["_composite"] = uses
                inner["_env"] = {**env, **{k: str(v) for k, v in (inner.get("env") or {}).items()}}
                inner["with"] = {
                    k: _subst(str(v), env, inputs) for k, v in (inner.get("with") or {}).items()
                }
                if "run" in inner:
                    inner["run"] = _subst(str(inner["run"]), env, inputs)
                # A composite step inherits the calling step's own conditions.
                inner["_caller"] = step
                out.append(inner)
        else:
            step = dict(step)
            step["_env"] = {**env, **{k: str(v) for k, v in (step.get("env") or {}).items()}}
            step["with"] = {k: _subst(str(v), env, {}) for k, v in (step.get("with") or {}).items()}
            out.append(step)
    return out


def _name(step: dict[str, Any]) -> str:
    return str(step.get("name") or step.get("uses") or "<unnamed>")


def _is_trivy(step: dict[str, Any]) -> bool:
    return str(step.get("uses") or "").startswith("aquasecurity/trivy-action")


def _is_gate(step: dict[str, Any]) -> bool:
    """A trivy step that can fail the job on findings (exit-code 1), not a report."""
    return _is_trivy(step) and str((step.get("with") or {}).get("exit-code", "0")) == "1"


def _pushes_to_production(step: dict[str, Any]) -> bool:
    uses = str(step.get("uses") or "")
    if uses.startswith("docker/build-push-action"):
        w = step.get("with") or {}
        if str(w.get("push", "")).lower() != "true":
            return False
        tags = [t.strip() for t in str(w.get("tags", "")).split() if t.strip()]
        return not tags or any(not t.startswith(CANDIDATE_HOSTS) for t in tags)
    return False


def _writes_to_production(step: dict[str, Any]) -> bool:
    uses = str(step.get("uses") or "")
    run = str(step.get("run") or "")
    return (
        any(uses.startswith(a) for a in WRITE_ACTIONS)
        or any(c in run for c in WRITE_COMMANDS)
        or _pushes_to_production(step)
    )


def _index(steps: list[dict[str, Any]], pred: Any) -> list[int]:
    return [i for i, s in enumerate(steps) if pred(s)]


def _build_platforms(steps: list[dict[str, Any]]) -> set[str]:
    builds = [s for s in steps if str(s.get("uses") or "").startswith("docker/build-push-action")]
    assert len(builds) == 1, f"expected exactly one build step, found {[_name(b) for b in builds]}"
    return {p.strip() for p in str(builds[0]["with"]["platforms"]).split(",") if p.strip()}


# --------------------------------------------------------------------------- non-vacuity


def test_the_detectors_see_the_release_path() -> None:
    steps = _steps()
    assert len(steps) >= 8, "the publish job parsed to almost nothing - the loader is broken"
    assert _index(steps, _writes_to_production), "no production-writing step detected"
    assert _index(steps, lambda s: str(s.get("uses") or "").startswith("docker/build-push-action"))
    assert len(_index(steps, _is_gate)) >= 2, (
        "fewer than two trivy gate steps (exit-code 1): the image ships two platforms, so a "
        "single gate cannot cover them"
    )


# --------------------------------------------------------------------------- ordering


def test_nothing_writes_to_a_production_registry_before_the_last_gate() -> None:
    steps = _steps()
    gates = _index(steps, _is_gate)
    assert gates, "no trivy gate step exists"
    last_gate = max(gates)
    early = [_name(steps[i]) for i in _index(steps, _writes_to_production) if i < last_gate]
    assert not early, (
        "these steps can write to GHCR / Docker Hub before the scan has passed: "
        f"{early}. A failing scan must leave no production tag, signature or attestation."
    )


def test_the_image_is_built_once_into_a_candidate_registry() -> None:
    steps = _steps()
    _build_platforms(steps)  # asserts exactly one build step
    pushing = [_name(s) for s in steps if _pushes_to_production(s)]
    assert not pushing, (
        f"{pushing} push to something other than the candidate registry "
        f"({CANDIDATE_HOSTS}). Production registries may only be written by the promote "
        "and tag steps, after the gate."
    )
    first_gate = min(_index(steps, _is_gate))
    build = _index(steps, lambda s: str(s.get("uses") or "").startswith("docker/build-push-action"))
    assert build[0] < first_gate, "the candidate must be built before it is scanned"


def test_production_tags_move_last_after_sign_and_attest() -> None:
    steps = _steps()
    sign = _index(steps, lambda s: "cosign sign" in str(s.get("run") or ""))
    attest = _index(steps, lambda s: str(s.get("uses") or "").startswith("actions/attest-build"))
    promote = _index(steps, lambda s: "release_image.py promote" in str(s.get("run") or ""))
    tag = _index(steps, lambda s: "release_image.py tag" in str(s.get("run") or ""))
    assert sign and attest and promote and tag, (
        f"expected promote/sign/attest/tag steps, found promote={promote} sign={sign} "
        f"attest={attest} tag={tag}"
    )
    assert max(promote) < min(sign), "sign the digest only after it was copied and verified"
    assert max(promote) < min(attest), "attest the digest only after it was copied and verified"
    assert max(sign) < min(tag) and max(attest) < min(tag), (
        "release tags (X.Y.Z, X.Y, latest) must move only after signing and attestation "
        "succeeded - otherwise a signing failure still leaves a published, unsigned release tag"
    )
    last_write = max(_index(steps, _writes_to_production))
    assert last_write == max(tag), (
        f"{_name(steps[last_write])!r} writes to production after the tag move"
    )


# --------------------------------------------------------------------------- scan coverage


def test_the_gate_scans_every_platform_the_build_ships() -> None:
    steps = _steps()
    shipped = _build_platforms(steps)
    scanned = {s["_env"].get("TRIVY_PLATFORM", "") for s in steps if _is_gate(s)} - {""}
    assert shipped <= scanned, (
        f"the build ships {sorted(shipped)} but the gate scans only {sorted(scanned)}. "
        "trivy scans the runner's platform when none is given, so an unscanned platform "
        "is an unscanned half of the release."
    )


def test_the_gate_scans_the_exact_digest_not_a_tag() -> None:
    steps = _steps()
    for s in (s for s in steps if _is_trivy(s)):
        ref = str(s["with"].get("image-ref", ""))
        assert re.search(r"@\$\{\{\s*steps\.build\.outputs\.digest\s*\}\}", ref), (
            f"{_name(s)!r} scans {ref!r}. A tag is mutable between the scan and the "
            "promotion; scan REPO@<build digest>."
        )
        assert "meta.outputs" not in ref, f"{_name(s)!r} scans a metadata-derived tag"


def test_gate_steps_cannot_fail_open() -> None:
    steps = _steps()
    for s in (s for s in steps if _is_gate(s)):
        n = _name(s)
        assert not s.get("continue-on-error"), f"{n!r} has continue-on-error"
        assert not s.get("if") and not (s.get("_caller") or {}).get("if"), (
            f"{n!r} is conditional ({s.get('if')!r}); the gate must run on every dispatch"
        )
        w = s["with"]
        assert str(w.get("exit-code")) == "1", f"{n!r} must exit 1 on findings"
        assert w.get("timeout") or s.get("timeout-minutes"), (
            f"{n!r} has no timeout: a hung scan must fail, not stall to the job cap"
        )
        assert "|| true" not in str(s.get("run") or ""), f"{n!r} swallows failures"


def test_a_failed_scan_cannot_be_papered_over_by_the_report_step() -> None:
    """Informational report steps may not run in place of the gate."""
    steps = _steps()
    shipped = _build_platforms(steps)
    gated = {s["_env"].get("TRIVY_PLATFORM", "") for s in steps if _is_gate(s)}
    for s in (s for s in steps if _is_trivy(s) and not _is_gate(s)):
        assert s["_env"].get("TRIVY_PLATFORM", "") in shipped | {""}, _name(s)
    assert shipped <= gated


# --------------------------------------------------------------------------- scan evidence


def _release_image_steps(steps: list[dict[str, Any]], sub: str) -> list[dict[str, Any]]:
    return [s for s in steps if f"release_image.py {sub}" in str(s.get("run") or "")]


def _platform_args(step: dict[str, Any]) -> set[str]:
    return set(re.findall(r"--platform\s+(\S+)", str(step.get("run") or "")))


def test_scan_steps_write_one_json_report_per_shipped_platform() -> None:
    """The evidence check reads trivy-report-<os>-<arch>.json; the scan must write exactly that."""
    steps = _steps()
    shipped = _build_platforms(steps)
    reports = [s for s in steps if _is_trivy(s) and not _is_gate(s)]
    for platform in sorted(shipped):
        want = "trivy-report-" + platform.replace("/", "-") + ".json"
        hits = [
            s
            for s in reports
            if s["with"].get("output") == want
            and s["with"].get("format") == "json"
            and s["_env"].get("TRIVY_PLATFORM") == platform
        ]
        assert hits, (
            f"no JSON report step writes {want!r} for {platform}: the evidence check "
            "(release_image.py check-reports) would have nothing to read"
        )
    for s in reports:
        assert str(s["with"].get("exit-code", "0")) == "0", (
            f"{_name(s)!r}: an evidence step that exits 1 would stop the job before the "
            "second platform's report exists"
        )
    last_report = max(_index(steps, lambda s: s in reports))
    assert last_report < min(_index(steps, _is_gate)), (
        "both platforms' reports must exist before the first gate can stop the job"
    )


def test_the_evidence_check_runs_after_the_gate_and_before_any_production_write() -> None:
    steps = _steps()
    shipped = _build_platforms(steps)
    check = _index(steps, lambda s: "release_image.py check-reports" in str(s.get("run") or ""))
    assert len(check) == 1, "exactly one evidence-check step is expected"
    i = check[0]
    assert i > max(_index(steps, _is_gate)), "the evidence check must follow every gate step"
    assert i < min(_index(steps, _writes_to_production)), (
        "the evidence check needs no credential and must fail the job before the first "
        "production login"
    )
    s = steps[i]
    assert _platform_args(s) == shipped, (
        f"the evidence check covers {sorted(_platform_args(s))}, the build ships {sorted(shipped)}"
    )
    assert "--report-dir" in str(s["run"])
    assert "steps.build.outputs.digest" in str(s.get("env") or "") + str(s["run"])
    assert not s.get("if") and not s.get("continue-on-error")


def test_promote_always_carries_the_scan_evidence_and_covers_every_platform() -> None:
    """``promote`` re-checks the reports itself: the copy cannot happen without them."""
    steps = _steps()
    shipped = _build_platforms(steps)
    promote = _release_image_steps(steps, "promote")
    assert promote
    for s in promote:
        assert "--report-dir" in str(s["run"]), f"{_name(s)!r} promotes without scan evidence"
        assert _platform_args(s) == shipped, (
            f"{_name(s)!r} verifies {sorted(_platform_args(s))}, the build ships {sorted(shipped)}"
        )
    for s in _release_image_steps(steps, "tag"):
        assert _platform_args(s) == shipped, f"{_name(s)!r} does not cover every platform"


# --------------------------------------------------------------------------- hygiene


def test_checkout_does_not_leave_the_token_in_the_workspace() -> None:
    """Every later step, including third-party actions above the gate, can read ``.git/config``."""
    steps = _steps()
    checkouts = [s for s in steps if str(s.get("uses") or "").startswith("actions/checkout")]
    assert checkouts
    for s in checkouts:
        assert str(s["with"].get("persist-credentials")).lower() == "false", (
            f"{_name(s)!r} persists the GITHUB_TOKEN in .git/config; nothing in this job pushes"
        )


def test_the_tag_set_is_checked_before_anything_is_built_copied_or_signed() -> None:
    """A non-semver v* tag or an empty tag list must fail in seconds, not after the copy."""
    steps = _steps()
    check = _index(steps, lambda s: "release_image.py check-tags" in str(s.get("run") or ""))
    assert len(check) == 1, "exactly one early tag-set check is expected"
    i = check[0]
    meta = _index(steps, lambda s: str(s.get("uses") or "").startswith("docker/metadata-action"))
    build = _index(steps, lambda s: str(s.get("uses") or "").startswith("docker/build-push-action"))
    assert meta and meta[0] < i < build[0], "after the metadata step, before the build"
    s = steps[i]
    assert not s.get("if") and not s.get("continue-on-error")
    blob = str(s.get("env") or "") + str(s["run"])
    assert "steps.meta.outputs.tags" in blob and "--immutable" in blob
    tag_step = _release_image_steps(steps, "tag")[0]
    assert str(s["env"]["IMMUTABLE"]) == str(tag_step["env"]["IMMUTABLE"]), (
        "the early check must use the same immutable-tag rule as the tag step"
    )


# --------------------------------------------------------------------------- one build, digest-addressed


def test_no_shell_step_builds_or_pushes_an_image() -> None:
    """The single build is ``docker/build-push-action``; a shell build after the gate would
    publish bytes nobody scanned and could not be told apart by the digest checks.

    The tool list below cannot be complete (nerdctl, ``ctr``, a script that wraps a build ...).
    It catches the obvious routes; the lasting guards are the digest checks (every production
    step takes the one build digest) and the exact-permission and single-build tests."""
    for s in _steps():
        run = str(s.get("run") or "")
        hit = re.search(
            r"\b(docker\s+(buildx\s+)?build|docker\s+push|buildctl|kaniko|podman\s+build)\b", run
        )
        assert not hit, f"{_name(s)!r} builds or pushes an image from the shell ({hit.group(0)!r})"


def test_cosign_signs_the_promoted_digest_not_a_tag() -> None:
    """A tag can move between the scan and the signature; the signature must bind the digest."""
    steps = _steps()
    build_digest = "${{ steps.build.outputs.digest }}"
    signs = [s for s in steps if re.search(r"\bcosign\s+(sign|attest)\b", str(s.get("run") or ""))]
    assert signs, "no cosign step found"
    for s in signs:
        run = str(s["run"])
        assert re.search(r"cosign\s+(sign|attest)\b[^\n]*@\$\{DIGEST\}", run), (
            f"{_name(s)!r} does not sign REPO@${{DIGEST}}: {run!r}"
        )
        assert s["_env"].get("DIGEST") == build_digest, (
            f"{_name(s)!r}: DIGEST is not the single build digest ({s['_env'].get('DIGEST')!r})"
        )
        assert not re.search(r"cosign\s+(sign|attest)\b[^\n]*(:\$\{\{|:latest|steps\.meta)", run)
    for s in (s for s in steps if str(s.get("uses") or "").startswith("actions/attest-build")):
        assert s["with"].get("subject-digest") == build_digest
        assert "@" not in str(s["with"].get("subject-name")) and ":" not in str(
            s["with"].get("subject-name")
        ).replace("steps.image.outputs.ref", "")


def _composite_guard_script() -> str:
    comp = _load(ROOT / ".github" / "actions" / "scan-candidate" / "action.yml")
    first = comp["runs"]["steps"][0]
    assert "run" in first, "the composite must start with its digest-only guard"
    return str(first["run"])


@pytest.mark.parametrize(
    ("ref", "ok"),
    [
        ("localhost:5000/x@sha256:" + "a" * 64, True),
        ("localhost:5000/x:candidate", False),
        ("localhost:5000/x", False),
        ("localhost:5000/x@sha256:" + "a" * 63, False),
        ("localhost:5000/x@sha256:" + "A" * 64, False),
        ("localhost:5000/x@sha256:" + "a" * 64 + "0", False),
        ("localhost:5000/x@sha256:", False),
        ("", False),
    ],
)
def test_the_scan_composite_refuses_anything_that_is_not_a_digest(ref: str, ok: bool) -> None:
    """The guard is executed, not grepped: a tag, an empty or a short digest must exit 1."""
    script = _composite_guard_script().replace("${{ inputs.image-ref }}", ref)
    done = subprocess.run(  # noqa: S603
        ["bash", "-c", script],  # noqa: S607
        env={"REF": ref, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert (done.returncode == 0) is ok, (ref, done.stdout, done.stderr)


def test_the_job_has_a_bounded_timeout() -> None:
    """Without ``timeout-minutes`` a hung copy or scan runs to GitHub's 6-hour default."""
    timeout = _load(WORKFLOW)["jobs"][JOB].get("timeout-minutes")
    assert isinstance(timeout, int) and 0 < timeout <= 60, timeout


def test_permissions_are_exactly_what_the_release_needs() -> None:
    """Nothing may widen silently: an extra ``contents: write`` lets the job push to the repo."""
    wf = _load(WORKFLOW)
    assert wf.get("permissions") == {"contents": "read"}
    assert wf["jobs"][JOB]["permissions"] == {
        "contents": "read",
        "packages": "write",  # GHCR push with GITHUB_TOKEN
        "id-token": "write",  # keyless cosign + attestation
        "attestations": "write",  # actions/attest-build-provenance
    }
    assert wf["jobs"]["ci-green"]["permissions"] == {"actions": "read", "contents": "read"}
    composite = _load(ROOT / ".github" / "actions" / "scan-candidate" / "action.yml")
    assert "permissions" not in composite


# --------------------------------------------------------------------------- bypass


def test_no_production_step_can_run_after_a_failure_or_a_cancel() -> None:
    steps = _steps()
    for i in _index(steps, _writes_to_production):
        cond = str(steps[i].get("if") or "") + str((steps[i].get("_caller") or {}).get("if") or "")
        n = _name(steps[i])
        assert not re.search(r"\b(always|failure|cancelled)\s*\(", cond), (
            f"{n!r} runs on {cond!r} - it could write to production after a failed gate"
        )
        assert not steps[i].get("continue-on-error"), f"{n!r} has continue-on-error"
        assert not re.search(r"\|\|\s*true|;\s*true\s*$", str(steps[i].get("run") or "")), n


def test_no_step_may_be_skipped_by_event_or_input() -> None:
    wf = _load(WORKFLOW)
    on = wf.get("on") or wf.get(True) or {}
    dispatch = on.get("workflow_dispatch") if isinstance(on, dict) else None
    assert not (dispatch or {}).get("inputs"), (
        "workflow_dispatch declares inputs; a `skip_scan`-style input is a bypass. "
        "Keep the dispatch inputless."
    )
    for s in _steps():
        cond = str(s.get("if") or "")
        assert not re.search(r"github\.event_name|github\.event\.inputs|inputs\.", cond), (
            f"{_name(s)!r} branches on the trigger ({cond!r}); a dispatch must take the "
            "same path as a tag push"
        )
    assert not any(
        isinstance(v, dict) and v.get("continue-on-error") is True for v in (wf["jobs"][JOB],)
    )


def test_every_conditional_step_is_a_known_optional_mirror_or_report() -> None:
    allowed = re.compile(r"^(env\.DOCKERHUB_MIRROR == 'true'|always\(\))$")
    odd = [
        (_name(s), s["if"])
        for s in _steps()
        if s.get("if") and not allowed.match(str(s["if"]).strip())
    ]
    assert not odd, f"unexpected conditional steps in the release job: {odd}"


def test_the_job_waits_for_green_ci_and_one_publish_runs_per_ref() -> None:
    wf = _load(WORKFLOW)
    needs = wf["jobs"][JOB].get("needs")
    needs = [needs] if isinstance(needs, str) else list(needs or [])
    assert "ci-green" in needs, "the publish job no longer depends on ADR-0316's CI gate"
    conc = wf.get("concurrency") or wf["jobs"][JOB].get("concurrency") or {}
    assert conc.get("group"), "no concurrency group: two runs can race on the moving tags"
    assert conc.get("cancel-in-progress") is False, (
        "cancel-in-progress must be false: cancelling mid-promotion leaves half a release"
    )


# --------------------------------------------------------------------------- registries


def test_promote_sign_attest_and_tag_use_the_one_build_digest() -> None:
    steps = _steps()
    for i in _index(steps, _writes_to_production):
        s = steps[i]
        if str(s.get("uses") or "").startswith("docker/login-action"):
            continue
        blob = str(s.get("run") or "") + str(s.get("with") or "") + str(s.get("_env") or "")
        env_blob = str(s.get("env") or "")
        assert "steps.build.outputs.digest" in blob + env_blob, (
            f"{_name(s)!r} does not take its digest from the single build step"
        )


def test_ghcr_and_docker_hub_are_promoted_and_tagged_by_separate_steps() -> None:
    steps = _steps()
    for kind in ("promote", "tag"):
        marked = [s for s in steps if f"release_image.py {kind}" in str(s.get("run") or "")]
        refs = [str(s.get("env") or "") + str(s.get("run") or "") for s in marked]
        assert any("ghcr.io" in r or "steps.image.outputs.ref" in r for r in refs), (
            f"no GHCR {kind} step"
        )
        assert any("docker.io" in r for r in refs), (
            f"no Docker Hub {kind} step: a mirror folded into the GHCR step cannot be "
            "gated, retried or failed on its own"
        )
        hub = [s for s in marked if "docker.io" in str(s.get("env") or "")]
        assert all(str(s.get("if")) == "env.DOCKERHUB_MIRROR == 'true'" for s in hub), (
            "the optional mirror must stay optional, and only through the token check"
        )


def test_the_candidate_registry_is_pinned_and_loopback_only() -> None:
    job = _load(WORKFLOW)["jobs"][JOB]
    registry = (job.get("services") or {}).get("registry") or {}
    assert re.search(r"@sha256:[0-9a-f]{64}$", str(registry.get("image", ""))), (
        "pin the candidate registry image by digest"
    )
    ports = [str(p) for p in registry.get("ports") or []]
    assert ports and all(p.startswith("127.0.0.1:") for p in ports), (
        f"candidate registry ports {ports} are not bound to loopback: an unscanned image "
        "must not be readable or writable from outside the job"
    )


def test_no_registry_secret_is_in_the_job_environment() -> None:
    """The Docker Hub token reaches the login step only, never the build or scan steps.

    A job-level ``env`` is inherited by every step, including third-party actions that run
    before the gate. Only the boolean "is the mirror configured" may live there.
    """
    wf = _load(WORKFLOW)
    job = wf["jobs"][JOB]
    for scope, env in (("workflow", wf.get("env") or {}), ("job", job.get("env") or {})):
        for key, value in env.items():
            if "secrets." in str(value):
                assert re.fullmatch(r"\$\{\{\s*secrets\.[A-Z_]+\s*!=\s*''\s*\}\}", str(value)), (
                    f"{scope} env {key} carries a secret value ({value!r}); every step, "
                    "including the ones above the gate, would inherit it. Pass the secret "
                    "to the one step that needs it and keep only a boolean here."
                )


@pytest.mark.parametrize("forbidden", ["docker/login-action", "attest-build-provenance"])
def test_no_other_job_can_publish_images(forbidden: str) -> None:
    wf = _load(WORKFLOW)
    others = {k: v for k, v in wf["jobs"].items() if k != JOB}
    text = str(others)
    assert forbidden not in text, (
        f"{forbidden} appears outside the gated {JOB!r} job; production writes must stay "
        "behind the single scan gate"
    )
