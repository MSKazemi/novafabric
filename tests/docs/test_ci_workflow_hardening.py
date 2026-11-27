"""Hardened CI workflows keep their job timeouts, SHA-pinned actions and read-only defaults (#217).

``docs/ci.md`` records the CI audit for issue #217. It found nine jobs with no
``timeout-minutes`` (a hung job then holds a runner for GitHub's 360-minute default) and
one workflow granting ``read-all`` at workflow level. Those were fixed in the files listed
in :data:`HARDENED`; this guard keeps them fixed.

Scope, stated rather than implied: only the workflows in :data:`HARDENED`. The other
non-release workflows already carried timeouts and pins when the audit ran, and are left
out so that a concurrent change to them is not blocked by this guard; the release and
publish workflows are owner-reviewed and listed as findings in ``docs/ci.md`` instead.
Add a file here once it meets all three rules below.

Rules, per hardened workflow:

1. every job that runs on a runner has a positive integer ``timeout-minutes`` no larger
   than :data:`MAX_TIMEOUT_MINUTES` (a reusable-workflow call has no runner and is exempt);
2. every ``uses:`` of a third-party action names a full 40-hex commit SHA and carries a
   ``# vX.Y.Z`` comment saying which release that SHA is (local ``./`` actions are exempt);
3. workflow-level ``permissions`` is present and grants nothing beyond ``read``/``none``
   (``read-all`` and ``write-all`` are refused; write access belongs on the one job that
   needs it).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"

#: Workflows hardened by the #217 audit. Each must satisfy every rule in this module.
HARDENED = (
    "cla.yml",
    "collector-ci.yml",
    "metadata_store_security_gate.yml",
    "scorecard.yml",
)

#: Upper bound for a job cap. GitHub's default is 360; a cap near it bounds nothing.
MAX_TIMEOUT_MINUTES = 120

_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>[^\s#]+)\s*(?:#\s*(?P<comment>.*))?$")
_PINNED = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
_VERSION_COMMENT = re.compile(r"^v?\d+(?:\.\d+){0,2}\b")


def _load(text: str) -> dict[str, Any]:
    doc = yaml.safe_load(text) or {}
    assert isinstance(doc, dict), "a workflow must be a YAML mapping"
    return doc


def timeout_violations(text: str) -> list[str]:
    """Jobs that run on a runner without a sane ``timeout-minutes``."""
    out = []
    for name, job in (_load(text).get("jobs") or {}).items():
        job = job or {}
        if "uses" in job:  # reusable-workflow call: the called job carries its own cap
            continue
        cap = job.get("timeout-minutes")
        if not isinstance(cap, int) or isinstance(cap, bool):
            out.append(f"{name}: no integer timeout-minutes (got {cap!r})")
        elif not 0 < cap <= MAX_TIMEOUT_MINUTES:
            out.append(f"{name}: timeout-minutes {cap} outside 1..{MAX_TIMEOUT_MINUTES}")
    return out


def pin_violations(text: str) -> list[str]:
    """``uses:`` lines naming a third-party action by tag/branch, or without a version comment."""
    out = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        match = _USES.match(line)
        if not match:
            continue
        ref = match.group("ref").strip("'\"")
        if ref.startswith("./"):
            continue
        if not _PINNED.match(ref):
            out.append(f"line {lineno}: {ref} is not pinned to a full commit SHA")
            continue
        comment = (match.group("comment") or "").strip()
        if not _VERSION_COMMENT.match(comment):
            out.append(f"line {lineno}: {ref} has no '# vX.Y.Z' comment naming its release")
    return out


def permission_violations(text: str) -> list[str]:
    """Workflow-level permissions that are missing or grant more than read."""
    doc = _load(text)
    if "permissions" not in doc:
        return ["no workflow-level permissions: the token inherits the repository default"]
    perms = doc["permissions"]
    if isinstance(perms, str):
        return [f"workflow-level permissions is {perms!r}; list read scopes explicitly"]
    if not isinstance(perms, dict):
        return [f"workflow-level permissions has unexpected shape {perms!r}"]
    return [
        f"workflow-level permission {scope}: {level}"
        for scope, level in perms.items()
        if level not in ("read", "none")
    ]


# ---------------------------------------------------------------------------
# The checks themselves are right: each catches what it exists to catch.
# ---------------------------------------------------------------------------

_GOOD = """
on: push
permissions:
  contents: read
jobs:
  a:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: ./.github/actions/capture
  gate:
    uses: ./.github/workflows/require-green-ci.yml
"""


def test_a_compliant_workflow_passes_every_check() -> None:
    assert timeout_violations(_GOOD) == []
    assert pin_violations(_GOOD) == []
    assert permission_violations(_GOOD) == []


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        ("    runs-on: ubuntu-24.04\n", "no integer timeout-minutes"),
        ("    runs-on: ubuntu-24.04\n    timeout-minutes: 0\n", "outside"),
        ("    runs-on: ubuntu-24.04\n    timeout-minutes: 360\n", "outside"),
        ("    runs-on: ubuntu-24.04\n    timeout-minutes: '10'\n", "no integer"),
        ("    runs-on: ubuntu-24.04\n    timeout-minutes: ${{ inputs.t }}\n", "no integer"),
    ],
)
def test_a_missing_or_unbounded_timeout_is_caught(job: str, expected: str) -> None:
    text = f"on: push\njobs:\n  a:\n{job}    steps: []\n"
    found = timeout_violations(text)
    assert len(found) == 1 and expected in found[0], found


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("      - uses: actions/checkout@v7\n", "not pinned"),
        ("      - uses: actions/checkout@main\n", "not pinned"),
        ("      - uses: actions/checkout@3d3c42e5\n", "not pinned"),
        ("        uses: docker://alpine:3.20\n", "not pinned"),
        ("      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n", "no '# vX"),
        (
            "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # latest\n",
            "no '# vX",
        ),
    ],
)
def test_a_tag_pin_or_an_unlabelled_sha_is_caught(line: str, expected: str) -> None:
    text = "jobs:\n  a:\n    steps:\n" + line
    found = pin_violations(text)
    assert len(found) == 1 and expected in found[0], found


@pytest.mark.parametrize(
    "perms",
    [
        "permissions: read-all\n",
        "permissions: write-all\n",
        "permissions:\n  contents: write\n",
        "",
    ],
)
def test_broad_or_missing_workflow_permissions_are_caught(perms: str) -> None:
    assert permission_violations(f"on: push\n{perms}jobs: {{}}\n")


def test_empty_workflow_permissions_are_least_privilege() -> None:
    assert permission_violations("on: push\npermissions: {}\njobs: {}\n") == []


# ---------------------------------------------------------------------------
# The hardened workflows satisfy them.
# ---------------------------------------------------------------------------


def test_every_hardened_workflow_exists() -> None:
    missing = [name for name in HARDENED if not (WORKFLOWS / name).is_file()]
    assert not missing, f"renamed or removed; update HARDENED and docs/ci.md: {missing}"


@pytest.mark.parametrize("name", HARDENED)
def test_hardened_workflow_jobs_have_timeouts(name: str) -> None:
    found = timeout_violations((WORKFLOWS / name).read_text())
    assert not found, f"{name}: " + "; ".join(found)


@pytest.mark.parametrize("name", HARDENED)
def test_hardened_workflow_actions_are_sha_pinned(name: str) -> None:
    found = pin_violations((WORKFLOWS / name).read_text())
    assert not found, f"{name}: " + "; ".join(found)


@pytest.mark.parametrize("name", HARDENED)
def test_hardened_workflow_defaults_to_read_only(name: str) -> None:
    found = permission_violations((WORKFLOWS / name).read_text())
    assert not found, f"{name}: " + "; ".join(found)


@pytest.mark.parametrize("name", HARDENED)
def test_the_ci_doc_lists_every_hardened_workflow(name: str) -> None:
    """docs/ci.md is the matrix; a hardened workflow it does not list has no audit record."""
    assert f"`{name}`" in (REPO / "docs" / "ci.md").read_text(), f"add {name} to docs/ci.md"
