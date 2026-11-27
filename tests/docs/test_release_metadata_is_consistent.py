"""The version must agree everywhere it is declared, and its notes must exist.

Cutting v0.102.0 on 2026-09-27 exposed that nothing tied these together:

* ``pyproject.toml`` and ``CITATION.cff`` both carry the version, and both were
  bumped by hand. A missed ``CITATION.cff`` would have published a citation
  record pointing at the wrong release, silently — v0.94.0 already shipped once
  with a missed ``pyproject.toml`` bump (ROADMAP, v0.95.0 row).
* ``docs/releases/v<version>.md`` is linked from README. For v0.102.0 the file
  existed on disk but was not yet tracked by the public git, so every reader of
  the published repository would have hit a dead link. ``test_doc_links`` caught
  that one only because the link was already written.

* ``deploy/helm/novafabric/Chart.yaml`` used to be exempt, on the grounds that
  ``publish-chart.yml`` overrides ``version`` and ``appVersion`` from the git tag.
  The in-repo values had nonetheless drifted to ``0.1.0`` / ``0.58.0`` while the
  package was at ``0.109.0`` — so ``helm install`` from a checkout pulled a
  months-old image by default, and ``helm template`` labelled every object
  with the wrong ``app.kubernetes.io/version``. Both fields now equal the
  package version, and ``publish-chart.yml`` refuses a tag that disagrees.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CHART_YAML = REPO_ROOT / "deploy" / "helm" / "novafabric" / "Chart.yaml"
PUBLISH_CHART = REPO_ROOT / ".github" / "workflows" / "publish-chart.yml"


def _project_version() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def _tracked_publicly(rel: str) -> bool:
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", rel],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def test_citation_version_matches_pyproject() -> None:
    version = _project_version()
    citation = (REPO_ROOT / "CITATION.cff").read_text(encoding="utf-8")
    match = re.search(r"^version:\s*(\S+)\s*$", citation, re.M)
    assert match, "CITATION.cff declares no version"
    assert match.group(1) == version, (
        f"CITATION.cff says {match.group(1)} but pyproject.toml says {version}. "
        "A citation record pointing at the wrong release is a published false "
        "statement about the artifact being cited."
    )


def test_readme_software_bibtex_title_matches_citation_cff() -> None:
    """One canonical software title: the README's @software BibTeX block had drifted
    from CITATION.cff ("... for {AI} and {HPC} runs"), giving two citations for one
    artifact. BibTeX brace-protection is ignored for the comparison."""
    citation = (REPO_ROOT / "CITATION.cff").read_text(encoding="utf-8")
    cff = re.search(r'^title:\s*"([^"]+)"\s*$', citation, re.M)
    assert cff, "CITATION.cff declares no top-level title"
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    block = re.search(r"@software\{novafabric,(.*?)\n\}", readme, re.S)
    assert block, "README has no @software{novafabric, ...} BibTeX block"
    bib = re.search(r"^\s*title\s*=\s*\{(.*)\},\s*$", block.group(1), re.M)
    assert bib, "README @software block has no title"
    assert bib.group(1).replace("{", "").replace("}", "") == cff.group(1)


def test_changelog_newest_release_matches_pyproject() -> None:
    version = _project_version()
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    # `## [Unreleased]` may sit above; the newest *version* heading is the release.
    headings = re.findall(r"^##\s*\[(\d+\.\d+\.\d+)\]", changelog, re.M)
    assert headings, "CHANGELOG.md has no version headings"
    assert headings[0] == version, (
        f"CHANGELOG's newest release heading is {headings[0]} but pyproject.toml "
        f"says {version}. Either the release notes were never opened for this "
        "version, or the bump landed without them."
    )


def test_release_notes_exist_and_are_publicly_tracked() -> None:
    version = _project_version()
    rel = f"docs/releases/v{version}.md"
    assert (REPO_ROOT / rel).is_file(), (
        f"{rel} is missing. README links the release notes by version, and the "
        "house style is one notes file per tag."
    )
    assert _tracked_publicly(rel), (
        f"{rel} exists on disk but is not tracked by the public git, so a reader "
        "of the published repository cannot open it. This exact state shipped a "
        "dead README link during the v0.102.0 preparation."
    )


def _chart() -> dict[str, object]:
    data = yaml.safe_load(CHART_YAML.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{CHART_YAML} is not a YAML mapping"
    return data


def test_chart_version_matches_pyproject() -> None:
    version = _project_version()
    chart_version = str(_chart().get("version"))
    assert chart_version == version, (
        f"Chart.yaml version is {chart_version} but pyproject.toml says {version}. "
        "Bump deploy/helm/novafabric/Chart.yaml together with pyproject.toml "
        "(docs/release-process.md step 6)."
    )


def test_chart_app_version_matches_pyproject() -> None:
    """appVersion is the default image tag: drift here deploys the wrong release."""
    version = _project_version()
    app_version = str(_chart().get("appVersion"))
    assert app_version == version, (
        f"Chart.yaml appVersion is {app_version} but pyproject.toml says {version}. "
        "With image.tag unset the chart deploys ghcr.io/...:<appVersion>, so a stale "
        "appVersion installs an old image from a current checkout."
    )


def test_chart_app_version_is_quoted() -> None:
    """An unquoted appVersion like 0.110 would parse as a float and lose digits."""
    text = CHART_YAML.read_text(encoding="utf-8")
    assert re.search(r'^appVersion:\s*"[^"]+"\s*$', text, re.M), (
        "Chart.yaml appVersion must be a quoted string"
    )


def test_publish_chart_refuses_a_tag_that_disagrees_with_chart_yaml() -> None:
    """The tag override and the in-repo values must not be able to diverge silently."""
    workflow = yaml.safe_load(PUBLISH_CHART.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["chart"]["steps"]
    names = [step.get("name", "") for step in steps]
    assert "Chart.yaml must match the tag" in names, (
        "publish-chart.yml lost the step that compares the tag to Chart.yaml"
    )
    check = names.index("Chart.yaml must match the tag")
    package = names.index("Package chart")
    assert check < package, "the tag check must run before the chart is packaged"
    script = steps[check]["run"]
    assert "Chart.yaml" in script and "exit 1" in script
