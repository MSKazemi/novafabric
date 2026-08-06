"""Every dependency manifest people clone must actually be watched.

Dependabot only looks where it is pointed. A manifest with no entry in
`.github/dependabot.yml` is not *partially* monitored and does not warn — it is
silently unmonitored forever, and the only visible symptom is the absence of
PRs, which is indistinguishable from having no updates available.

That is how this repository ended up publishing vulnerable dependencies while
the root lockfile was kept clean: the root `uv.lock` was watched and fixed,
while `examples/plugin-hook-reference/uv.lock` (cryptography 49.0.0),
`packages/nova-sdk-ts` (brace-expansion 5.0.8, postcss 8.5.19),
`packages/nova-dashboard` (postcss 8.5.19) and the whole of `collector/go.mod`
were not watched at all. Three of those four are files a reader of the public
repository clones.

This is the same failure the rest of `tests/docs/` guards against, in a
different costume: **a check that only runs where you work is not a check.**

The test derives the manifest list from git rather than from a hardcoded list,
so adding a new sub-project to the repository fails here until it is also added
to the Dependabot config.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPENDABOT_CONFIG = REPO_ROOT / ".github" / "dependabot.yml"

# Which ecosystem is responsible for a given manifest filename. A directory is
# considered covered if *any* of its manifests' ecosystems is configured for it.
_ECOSYSTEM_BY_MANIFEST = {
    "uv.lock": "uv",
    "pyproject.toml": "uv",
    "package-lock.json": "npm",
    "package.json": "npm",
    "go.mod": "gomod",
    "Dockerfile": "docker",
}

# `github-actions` is configured once at "/" and covers .github/workflows/*.
# Workflow files are therefore not treated as per-directory manifests.
_ACTIONS_ECOSYSTEM = "github-actions"


def _tracked_files() -> list[str]:
    """Paths the *public* git tracks — what a reader actually clones.

    Deliberately not `Path.glob`: a manifest sitting in the working tree but
    absent from the public git is not something anyone else receives, and a
    manifest tracked but not present locally still must be watched.
    """
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _configured_pairs() -> set[tuple[str, str]]:
    """(ecosystem, directory) pairs declared in the config.

    Handles both the singular `directory` key and the plural `directories`
    list; using one and forgetting the other is an easy way to think a path is
    covered when it is not.
    """
    config = yaml.safe_load(DEPENDABOT_CONFIG.read_text(encoding="utf-8"))
    pairs: set[tuple[str, str]] = set()

    for entry in config.get("updates", []):
        ecosystem = entry.get("package-ecosystem")
        if not ecosystem:
            continue

        directories = list(entry.get("directories") or [])
        if entry.get("directory"):
            directories.append(entry["directory"])

        for directory in directories:
            # Normalise "/", "/web", "web/" to a comparable form.
            pairs.add((ecosystem, "/" + directory.strip("/")))

    return pairs


def _manifest_requirements() -> dict[tuple[str, str], list[str]]:
    """Map each required (ecosystem, directory) to the manifests demanding it."""
    required: dict[tuple[str, str], list[str]] = {}

    for path in _tracked_files():
        name = Path(path).name
        ecosystem = _ECOSYSTEM_BY_MANIFEST.get(name)

        # Dockerfile.foo / foo.Dockerfile still describe a base image.
        if ecosystem is None and "Dockerfile" in name:
            ecosystem = "docker"
        if ecosystem is None:
            continue

        parent = str(Path(path).parent)
        directory = "/" if parent == "." else "/" + parent
        required.setdefault((ecosystem, directory), []).append(path)

    return required


def test_dependabot_config_is_valid_yaml_and_version_2() -> None:
    config = yaml.safe_load(DEPENDABOT_CONFIG.read_text(encoding="utf-8"))

    assert config.get("version") == 2, "Dependabot requires `version: 2`"
    assert config.get("updates"), "no `updates` entries — nothing is monitored at all"


def test_github_actions_are_monitored() -> None:
    """The workflows are a supply chain too — a pinned action can be yanked."""
    assert (_ACTIONS_ECOSYSTEM, "/") in _configured_pairs(), (
        "no `github-actions` entry at '/': the workflows in .github/workflows/ "
        "are unmonitored"
    )


@pytest.mark.parametrize(
    ("ecosystem", "directory"),
    sorted(_manifest_requirements()),
    ids=lambda value: str(value),
)
def test_every_tracked_manifest_is_covered(ecosystem: str, directory: str) -> None:
    """Each manifest directory must appear in the Dependabot config.

    If this fails, do not delete the manifest or narrow the test — add the
    entry. An unwatched manifest ships CVEs to everyone who clones the repo.
    """
    configured = _configured_pairs()
    manifests = _manifest_requirements()[(ecosystem, directory)]

    # A pyproject.toml with no lockfile beside it may legitimately be resolved
    # by a parent workspace, so accept coverage of the repository root too.
    covered = (ecosystem, directory) in configured
    if not covered and all(Path(m).name == "pyproject.toml" for m in manifests):
        covered = (ecosystem, "/") in configured

    assert covered, (
        f"{', '.join(manifests)} is tracked by the public git but no "
        f"`{ecosystem}` entry in .github/dependabot.yml points at "
        f"'{directory}'. Dependabot will never propose a security update for "
        f"it, and will never tell you that."
    )
