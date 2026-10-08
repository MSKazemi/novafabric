"""Documented GHCR paths must name the namespace CI actually publishes to.

Regression guard for a defect found on 2026-09-27, after the repository moved
from the ``novafabric`` organisation to ``MSKazemi``:

* ``publish-chart.yml`` hardcoded ``oci://ghcr.io/novafabric/charts``. A
  repo-scoped ``GITHUB_TOKEN`` cannot write another owner's package, so the
  v0.102.0 chart push failed outright while ``publish-image.yml`` — which
  resolves ``ghcr.io/${{ github.repository }}`` — kept working.
* Eighteen live references across ``docs/ops/``, ``deploy/helm/`` and
  ``deploy/k8s/`` still named the old namespace, including the chart's own
  default ``image.repository``. Every documented ``docker pull`` returned 401
  and a plain ``helm install`` produced ImagePullBackOff.

Nothing tied the two together, so a registry path could be true in CI and false
in the documentation indefinitely. Both halves are asserted here:

1. No workflow hardcodes a GHCR namespace — it is derived from the repository.
2. Every live doc/deploy reference uses the owner of the public remote.

Historical release notes under ``docs/releases/`` are exempt: they record what
was true when that version shipped and must not be rewritten.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: ``ghcr.io/<namespace>/`` — the namespace is the part that can drift.
GHCR_PATH = re.compile(r"ghcr\.io/([A-Za-z0-9._-]+)/")

#: Release notes are a historical record, not live instructions.
EXEMPT_PREFIXES = ("docs/releases/",)

#: Namespaces that are not ours and are legitimately referenced. Kept explicit
#: rather than pattern-matched so that adding one is a deliberate review step.
#:   devcontainers - the devcontainer feature images
#:   astral-sh     - the uv base image in deploy/docker/Dockerfile
THIRD_PARTY = {"devcontainers", "astral-sh"}


def _tracked(*paths: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", *paths],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in out.splitlines() if not p.startswith(EXEMPT_PREFIXES)]


def _expected_namespace() -> str:
    """The owner of the public remote, lowercased as GHCR stores it."""
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # git@github.com:OWNER/repo.git  or  https://github.com/OWNER/repo.git
    owner = re.sub(r"^.*[:/]([^/]+)/[^/]+?(\.git)?$", r"\1", url)
    assert owner and owner != url, f"could not parse an owner from {url!r}"
    return owner.lower()


def test_no_workflow_hardcodes_a_ghcr_namespace() -> None:
    """A hardcoded namespace is what broke the v0.102.0 chart push."""
    offenders: list[str] = []
    for rel in _tracked(".github/workflows"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        for line_no, line in enumerate(text.splitlines(), start=1):
            # A comment cannot push anything. Workflow prose legitimately quotes
            # the broken reference this guard exists to prevent, and a guard that
            # fails on an accurate explanation is one people learn to suppress.
            if line.strip().startswith("#"):
                continue
            for match in GHCR_PATH.finditer(line):
                ns = match.group(1)
                if ns in THIRD_PARTY or ns.startswith("$"):
                    continue
                offenders.append(f"{rel}:{line_no}  ghcr.io/{ns}/")

    assert not offenders, (
        "a workflow hardcodes a GHCR namespace instead of deriving it from the "
        "repository. A repo-scoped GITHUB_TOKEN cannot write another owner's "
        "package, so this fails the moment the repository moves:\n  "
        + "\n  ".join(offenders)
    )


def test_live_docs_name_the_namespace_ci_publishes_to() -> None:
    expected = _expected_namespace()
    offenders: list[str] = []
    # integrations/ and collector/ were outside this scan until 2026-10-08, when the
    # Claude plugin's deploy skill was found still naming ghcr.io/novafabric/.
    for rel in _tracked("docs", "deploy", "README.md", "examples", "integrations", "collector"):
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line_no, line in enumerate(text.splitlines(), start=1):
            for match in GHCR_PATH.finditer(line):
                ns = match.group(1)
                if ns in THIRD_PARTY or ns == expected:
                    continue
                offenders.append(f"{rel}:{line_no}  ghcr.io/{ns}/ (expected {expected})")

    assert not offenders, (
        f"live documentation names a GHCR namespace other than {expected!r}, the "
        "owner of the public remote. Readers get 401 on `docker pull` and "
        "ImagePullBackOff on `helm install`:\n  " + "\n  ".join(offenders)
    )


def test_no_workflow_builds_an_oci_ref_without_lowercasing() -> None:
    """An OCI repository name must be lowercase; ``github.repository`` is not.

    v0.102.0's image published but shipped UNSIGNED: the push tags come from
    docker/metadata-action, which lowercases its ``images:`` input, but the raw
    expression handed to cosign did not, and the step died on

        Error: signing [ghcr.io/MSKazemi/novafabric@sha256:...]:
               parsing reference: could not parse reference

    The same raw form also fed the trivy CRITICAL-vulnerability release gate, so
    that gate could not have resolved its image either. A ``images:`` input to
    metadata-action is exempt because the action lowercases it itself.
    """
    offenders: list[str] = []
    for rel in _tracked(".github/workflows"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        for line_no, line in enumerate(text.splitlines(), start=1):
            if "github.repository }}" not in line or "ghcr.io/" not in line:
                continue
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # prose about this very defect
            if "[:lower:]" in line:
                continue  # the line doing the lowercasing
            if stripped.startswith("images:") or stripped.startswith("- ghcr.io/"):
                continue  # metadata-action lowercases its own input
            if re.match(r"^ghcr\.io/\$\{\{ github\.repository \}\}$", stripped):
                continue  # bare metadata-action `images:` list item
            offenders.append(f"{rel}:{line_no}  {stripped[:90]}")

    assert not offenders, (
        "a workflow builds an OCI reference from `github.repository` without "
        "lowercasing it. OCI repository names must be lowercase, and this owner "
        "is not — cosign and trivy cannot parse the result:\n  "
        + "\n  ".join(offenders)
    )


def test_the_sweep_is_not_vacuous() -> None:
    """Both checks must actually be reading files."""
    assert len(_tracked(".github/workflows")) > 5
    assert len(_tracked("docs", "deploy", "README.md", "examples")) > 50
    assert _expected_namespace() == "mskazemi"


def test_collector_go_module_path_is_where_its_code_lives() -> None:
    """``go install <module>/cmd/...@latest`` needs the module path to be fetchable.

    Found 2026-10-08: ``collector/go.mod`` declared ``github.com/novafabric/collector``.
    Nothing is published at that path (the Go proxy answers 404), and fetching the
    real location failed on the path mismatch, so every documented ``go install``
    was dead. The module path must be the public repository plus ``/collector``.
    """
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    owner_repo = re.sub(r"^.*github\.com[:/]([^/]+/[^/]+?)(\.git)?$", r"\1", url)
    assert owner_repo != url, f"could not parse owner/repo from {url!r}"
    first = (REPO_ROOT / "collector" / "go.mod").read_text(encoding="utf-8").splitlines()[0]
    assert first == f"module github.com/{owner_repo}/collector", (
        f"collector/go.mod declares {first!r}; `go install` resolves the module path "
        f"as a URL, so it must be 'module github.com/{owner_repo}/collector'"
    )
