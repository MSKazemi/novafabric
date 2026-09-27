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

``deploy/helm/novafabric/Chart.yaml`` is deliberately NOT checked. Its own
comments record that ``publish-chart.yml`` overrides both ``version`` and
``appVersion`` from the git tag at release time, so the in-repo values are
placeholders by design, not drift.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


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
