"""Support-policy and release-gate documentation guards (ADR-0245 slice 1).

Three drift classes this file makes impossible to commit silently:

1. ``SECURITY.md`` and ``docs/support-policy.md`` disagreeing about what is
   supported (the policy page is the source of truth and SECURITY.md must link it).
2. The support matrix's Python floor drifting from ``requires-python`` in
   ``pyproject.toml``.
3. A CI job existing that ``docs/release-process.md`` does not document — the
   release-gate doc must name every job, the same only-a-mechanism-is-a-promise
   rule that ``test_makefile_matches_ci_gate.py`` enforces for the unit command.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
POLICY = REPO / "docs" / "support-policy.md"
SECURITY = REPO / "SECURITY.md"
RELEASE_PROCESS = REPO / "docs" / "release-process.md"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
PYPROJECT = REPO / "pyproject.toml"


def _ci_job_ids() -> list[str]:
    """Top-level job ids from ci.yml, without a YAML dependency.

    Job ids are the 2-space-indented ``name:``-style keys under ``jobs:``.
    """
    lines = CI_WORKFLOW.read_text().splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.rstrip() == "jobs:")
    except StopIteration:  # pragma: no cover - ci.yml always has jobs
        raise AssertionError("ci.yml has no top-level 'jobs:' key") from None
    ids = []
    for ln in lines[start + 1 :]:
        if ln and not ln.startswith(" "):
            break  # left the jobs: mapping
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", ln)
        if m:
            ids.append(m.group(1))
    assert ids, "parsed zero job ids from ci.yml — parser or workflow changed shape"
    return ids


# Every CI job must be documented in release-process.md. The value lists the
# marker strings (any one suffices) that count as documentation of that job.
# Adding a CI job without extending this mapping AND the doc fails below by name.
_JOB_DOC_MARKERS: dict[str, list[str]] = {
    "unit": ["--cov=novafabric"],
    "typecheck": ["mypy src"],
    "seal-latency-gate": ["seal_latency", "NovaSeal p99"],
    "capture-overhead-gate": ["capture-overhead-gate"],
    "web": ["`web`"],
    "integration": ["`integration`"],
}


def test_every_ci_job_is_documented_in_the_release_process() -> None:
    doc = RELEASE_PROCESS.read_text()
    undocumented = []
    unmapped = []
    for job in _ci_job_ids():
        markers = _JOB_DOC_MARKERS.get(job)
        if markers is None:
            unmapped.append(job)
        elif not any(m in doc for m in markers):
            undocumented.append(job)
    assert not unmapped, (
        f"CI jobs {unmapped} are not in _JOB_DOC_MARKERS — document each in "
        "docs/release-process.md and add its marker here"
    )
    assert not undocumented, (
        f"CI jobs {undocumented} have no matching marker in docs/release-process.md — "
        "the documented release gate must name every CI job"
    )


def test_doc_marker_mapping_has_no_stale_jobs() -> None:
    """The other direction: a mapping entry for a deleted CI job is stale."""
    stale = set(_JOB_DOC_MARKERS) - set(_ci_job_ids())
    assert not stale, f"_JOB_DOC_MARKERS names CI jobs that no longer exist: {sorted(stale)}"


def test_security_md_links_the_support_policy() -> None:
    assert "docs/support-policy.md" in SECURITY.read_text(), (
        "SECURITY.md 'Supported versions' must link docs/support-policy.md"
    )


def test_policy_and_security_agree_on_pre10_stance() -> None:
    policy = " ".join(POLICY.read_text().split())
    security = " ".join(SECURITY.read_text().split())
    for text, name in ((policy, "support-policy.md"), (security, "SECURITY.md")):
        assert "latest tagged release is supported" in text, (
            f"{name} no longer states the latest-only pre-1.0 stance; "
            "if the policy changed, change both files together"
        )
        assert "LTS line before v1.0" in text, name


def test_python_floor_matches_pyproject() -> None:
    m = re.search(r'requires-python\s*=\s*"\s*>=\s*([\d.]+)"', PYPROJECT.read_text())
    assert m, "could not parse requires-python from pyproject.toml"
    floor = m.group(1)
    assert f"≥ {floor}" in POLICY.read_text(), (
        f"docs/support-policy.md must state the Python floor '≥ {floor}' "
        "matching pyproject.toml requires-python"
    )
