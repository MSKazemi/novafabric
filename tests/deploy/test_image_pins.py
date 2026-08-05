"""Drift-prevention test for pinned third-party container images.

Enforces `deploy/IMAGE_PINS.md` as the single documented source of truth for
the image tags this project has deliberately converged on: `janusgraph/janusgraph`,
`edoburu/pgbouncer`, and `apache/age` (a packaging audit found five
inconsistent JanusGraph version references, plus two `:latest` pins on
pgbouncer/AGE, scattered across compose files, the Helm chart, and tests).

Scope is intentionally limited to those three tracked image families. This is
not a blanket "no `:latest` anywhere in the repo" scanner — that would also
need to touch files well outside this pass's purpose (e.g. the
`ghcr.io/astral-sh/uv:latest` installer stage in `deploy/docker/Dockerfile`,
or the not-yet-published `ghcr.io/novafabric/novafabric-collector:latest`
image in `deploy/k8s/`), which would be unrelated version-pin churn. See
`deploy/IMAGE_PINS.md`'s "Notes" section for the same scoping statement.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE_PINS_MD = REPO_ROOT / "deploy" / "IMAGE_PINS.md"

COMPOSE_FILES = [
    REPO_ROOT / "deploy" / "docker" / "docker-compose.yml",
    REPO_ROOT / "tests" / "integration" / "docker-compose.eval.yaml",
]
LINEAGE_TEST_FILES = [
    REPO_ROOT / "tests" / "lineage" / "test_janusgraph_backend.py",
    REPO_ROOT / "tests" / "lineage" / "test_age_backend.py",
]
HELM_CHART_DIRS = sorted(p for p in (REPO_ROOT / "deploy" / "helm").glob("*") if p.is_dir())

# The three image families this pass converges on. Order matters for the
# regex alternation (none is a prefix of another here, so order is free).
TRACKED_REPOS = ("janusgraph/janusgraph", "edoburu/pgbouncer", "apache/age")

_IMAGE_REF_RE = re.compile(
    r"(" + "|".join(re.escape(repo) for repo in TRACKED_REPOS) + r"):([A-Za-z0-9_.\-]+)"
)


def _load_pins() -> dict[str, str]:
    """Parse the {image: tag} table out of deploy/IMAGE_PINS.md."""
    pins: dict[str, str] = {}
    for line in IMAGE_PINS_MD.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        image_match = re.search(r"`([^`]+)`", cells[0])
        tag_match = re.search(r"`([^`]+)`", cells[1])
        if image_match and tag_match and image_match.group(1) in TRACKED_REPOS:
            pins[image_match.group(1)] = tag_match.group(1)
    return pins


PINS = _load_pins()


def _image_refs(path: Path) -> list[tuple[str, str]]:
    """Every (repo, tag) pair found anywhere in a file's raw text."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    return _IMAGE_REF_RE.findall(text)


def test_image_pins_md_documents_every_tracked_image() -> None:
    assert set(PINS) == set(TRACKED_REPOS), (
        f"deploy/IMAGE_PINS.md must document exactly {sorted(TRACKED_REPOS)}, "
        f"found {sorted(PINS)}"
    )


def test_no_documented_pin_is_latest() -> None:
    for repo, tag in PINS.items():
        assert tag != "latest", f"deploy/IMAGE_PINS.md pins {repo} to :latest"


@pytest.mark.parametrize(
    "path", COMPOSE_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_compose_file_images_match_pins(path: Path) -> None:
    assert path.is_file(), f"expected compose file is missing: {path}"
    refs = _image_refs(path)
    assert refs, f"{path}: expected at least one tracked image reference"
    for repo, tag in refs:
        assert tag != "latest", f"{path}: {repo} is pinned to :latest"
        assert tag == PINS[repo], (
            f"{path}: {repo} is pinned to {tag!r}, but deploy/IMAGE_PINS.md "
            f"says {PINS[repo]!r}"
        )


@pytest.mark.parametrize(
    "path", LINEAGE_TEST_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_lineage_backend_test_images_match_pins(path: Path) -> None:
    assert path.is_file(), f"expected test file is missing: {path}"
    refs = _image_refs(path)
    assert refs, f"{path}: expected at least one tracked image reference"
    for repo, tag in refs:
        assert tag != "latest", f"{path}: {repo} is pinned to :latest"
        assert tag == PINS[repo], (
            f"{path}: {repo} is pinned to {tag!r}, but deploy/IMAGE_PINS.md "
            f"says {PINS[repo]!r}"
        )


@pytest.mark.parametrize("chart_dir", HELM_CHART_DIRS, ids=lambda p: p.name)
def test_helm_chart_tracked_images_match_pins(chart_dir: Path) -> None:
    values_path = chart_dir / "values.yaml"
    if not values_path.is_file():
        pytest.skip(f"{chart_dir.name}: no values.yaml")
    values = yaml.safe_load(values_path.read_text(encoding="utf-8")) or {}
    image = values.get("image") or {}
    repo = image.get("repository")
    if repo not in TRACKED_REPOS:
        pytest.skip(f"{chart_dir.name}: image.repository {repo!r} is not a tracked image")

    tag = image.get("tag")
    assert tag, f"{values_path}: image.tag must be set for tracked image {repo}"
    assert tag != "latest", f"{values_path}: {repo} image.tag must not be :latest"
    assert tag == PINS[repo], (
        f"{values_path}: {repo} pinned to {tag!r}, but deploy/IMAGE_PINS.md says {PINS[repo]!r}"
    )

    chart_path = chart_dir / "Chart.yaml"
    if chart_path.is_file():
        chart = yaml.safe_load(chart_path.read_text(encoding="utf-8")) or {}
        app_version = chart.get("appVersion")
        assert app_version == tag, (
            f"{chart_path}: appVersion {app_version!r} must track values.yaml "
            f"image.tag {tag!r}"
        )


def test_no_tracked_image_pinned_to_latest_anywhere_under_deploy_or_tests() -> None:
    """The actual sweep: no tracked image is pinned to :latest anywhere.

    Scans every text-ish file under `deploy/**` and `tests/**` for a tracked
    image repo immediately followed by `:latest`. Scoped to `TRACKED_REPOS`
    (see module docstring) rather than every image reference in the repo.
    """
    offenders: list[str] = []
    for root_name in ("deploy", "tests"):
        root = REPO_ROOT / root_name
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in {".yml", ".yaml", ".py", ".md", ".ini"}:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for repo in TRACKED_REPOS:
                if f"{repo}:latest" in text:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {repo}:latest")
    assert not offenders, "tracked image(s) pinned to :latest:\n" + "\n".join(offenders)
