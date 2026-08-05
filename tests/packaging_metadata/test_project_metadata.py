"""PyPI packaging metadata checks (S1 — additive PyPI metadata + new extras).

Guards the ``[project]`` table in ``pyproject.toml`` against the class of bug this
slice fixes: metadata that *looks* plausible but is subtly wrong — a misspelled
trove classifier, a Python-version classifier that has drifted from what CI
actually tests, a ``readme`` pointer to a file that does not exist, or a new
optional extra that silently stops existing (or silently starts pulling in
something it should not).

These are structural/static checks only — no network access, no ``pip install``
or ``uv build`` subprocess (that is covered by the manual `` uv build && uv run
twine check dist/*`` gate documented in CONTRIBUTING.md and run in CI's
``publish-pypi.yml``).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
from trove_classifiers import classifiers as VALID_TROVE_CLASSIFIERS

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"
_CI_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Cloud-vendor SDK extras: each locks a deployment into one specific cloud
# vendor's client library. `all` must not force every installer to pull in
# all three vendors' SDKs by default.
_CLOUD_VENDOR_EXTRAS = {
    "worm-s3",
    "worm-azure",
    "worm-gcs",
    "seal-aws",
    "seal-azure",
    "seal-gcp",
}
# Agent-framework adapter extras: each targets a distinct, mutually exclusive
# third-party agent framework.
_AGENT_FRAMEWORK_EXTRAS = {
    "openai-agents",
    "google-adk",
    "bedrock-agentcore",
    "a2a",
}


def _load_pyproject() -> dict[str, Any]:
    with _PYPROJECT_PATH.open("rb") as f:
        return tomllib.load(f)


def _project_table() -> dict[str, Any]:
    return _load_pyproject()["project"]


def test_readme_field_points_at_a_real_file() -> None:
    project = _project_table()
    assert project.get("readme") == "README.md"
    assert (_REPO_ROOT / project["readme"]).is_file()


def test_authors_field_is_present_and_well_formed() -> None:
    authors = _project_table().get("authors")
    assert authors, "expected at least one [project] author entry"
    for author in authors:
        # PEP 621 allows name-only, email-only, or both; require at least one.
        assert author.get("name") or author.get("email")


def test_keywords_present_and_nonempty() -> None:
    keywords = _project_table().get("keywords")
    assert keywords, "expected a non-empty [project] keywords list"
    assert all(isinstance(k, str) and k for k in keywords)


def test_project_urls_cover_the_required_link_set() -> None:
    urls = _load_pyproject()["project"].get("urls", {})
    for required in ("Homepage", "Documentation", "Repository", "Changelog", "Issues"):
        assert required in urls, f"[project.urls] is missing {required!r}"
        assert urls[required].startswith("https://")


def test_classifiers_are_all_valid_trove_classifiers() -> None:
    """Every string in `classifiers` must be a real, exactly-spelled trove classifier.

    A typo here (e.g. a stray space, wrong "::" count, wrong capitalization) is
    silently accepted by ``hatchling`` at build time and only surfaces as a
    rejected/ignored classifier on the live PyPI project page — this catches it
    locally instead.
    """
    classifiers = _project_table().get("classifiers", [])
    assert classifiers, "expected a non-empty [project] classifiers list"
    invalid = [c for c in classifiers if c not in VALID_TROVE_CLASSIFIERS]
    assert not invalid, f"not real trove classifiers: {invalid!r}"


def test_classifiers_declare_beta_status_and_apache_license() -> None:
    classifiers = set(_project_table()["classifiers"])
    assert "Development Status :: 4 - Beta" in classifiers
    assert "License :: OSI Approved :: Apache Software License" in classifiers


def test_python_version_classifiers_match_the_ci_matrix_exactly() -> None:
    """The declared Python classifiers must equal what CI actually exercises.

    Claiming a Python version PyPI-side that CI never runs (e.g. adding 3.13
    speculatively) is exactly the kind of overclaim the docs-honesty rule in
    CLAUDE.md forbids for documentation, and the same standard applies to
    package metadata: don't advertise support the test suite doesn't verify.
    """
    ci_text = _CI_WORKFLOW_PATH.read_text()
    ci_python_versions = set(re.findall(r'python-version:\s*"([0-9.]+)"', ci_text))
    assert ci_python_versions, "could not find any python-version entries in ci.yml"

    classifiers = _project_table()["classifiers"]
    declared_versions = {
        c.rsplit("::", 1)[1].strip()
        for c in classifiers
        if re.fullmatch(r"Programming Language :: Python :: \d+\.\d+", c)
    }

    assert declared_versions == ci_python_versions, (
        f"pyproject.toml declares Python classifiers {declared_versions!r} but "
        f"ci.yml's matrix only tests {ci_python_versions!r} — these must match exactly"
    )
    # Every declared per-minor-version classifier must have taken effect via a
    # real entry, i.e. this must not be vacuously empty.
    assert declared_versions == {"3.12"}


def test_no_python_3_13_classifier_yet() -> None:
    """Explicit regression guard: CI is Python-3.12-only as of this slice.

    Fails loudly (rather than via the more general set-equality check above)
    the moment someone adds "Programming Language :: Python :: 3.13" without
    CI actually testing 3.13, which is the exact mistake this slice's brief
    called out to avoid.
    """
    classifiers = _project_table()["classifiers"]
    assert "Programming Language :: Python :: 3.13" not in classifiers


def test_no_non_linux_os_classifier() -> None:
    """CI (.github/workflows/ci.yml) only runs `ubuntu-latest` — no macOS/Windows job."""
    classifiers = _project_table()["classifiers"]
    for c in classifiers:
        if c.startswith("Operating System ::"):
            assert c == "Operating System :: POSIX :: Linux", (
                f"unexpected non-Linux OS classifier {c!r}; CI does not test "
                "macOS or Windows"
            )
    ci_text = _CI_WORKFLOW_PATH.read_text()
    assert "macos" not in ci_text.lower()
    assert "windows" not in ci_text.lower()


def test_typing_typed_classifier_only_present_because_py_typed_exists() -> None:
    classifiers = _project_table()["classifiers"]
    py_typed = _REPO_ROOT / "src" / "novafabric" / "py.typed"
    if "Typing :: Typed" in classifiers:
        assert py_typed.is_file(), (
            "'Typing :: Typed' classifier is declared but src/novafabric/py.typed is missing"
        )


def test_py_typed_marker_file_exists_and_is_empty() -> None:
    py_typed = _REPO_ROOT / "src" / "novafabric" / "py.typed"
    assert py_typed.is_file()
    assert py_typed.read_text() == ""


def test_py_typed_ships_as_installed_package_data() -> None:
    """Confirms py.typed resolves through the real installed/importable package.

    Runs against whatever `novafabric` is importable in this environment (an
    editable install under `uv sync`, matching how the project is actually
    tested) rather than re-invoking `pip install -e .` inside the test, which
    would be redundant with the environment pytest is already running in.
    """
    import importlib.resources

    marker = importlib.resources.files("novafabric").joinpath("py.typed")
    assert marker.is_file()


def test_wheel_build_includes_py_typed() -> None:
    include = _load_pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]["include"]
    assert "src/novafabric/py.typed" in include


@pytest.mark.parametrize(
    ("extra_name", "expected_package"),
    [
        ("clickhouse", "clickhouse-connect"),
        ("nats", "nats-py"),
        ("avro", "fastavro"),
        ("energy-gpu", "nvidia-ml-py"),
    ],
)
def test_new_narrow_extras_exist_and_provide_the_expected_package(
    extra_name: str, expected_package: str
) -> None:
    """These extras must exist for the runtime ImportError hints that already
    reference them (e.g. ``evidence_fabric/clickhouse_accumulator.py``'s
    "Install it with: pip install novafabric[clickhouse]") to be true.
    """
    optional_deps = _load_pyproject()["project"]["optional-dependencies"]
    assert extra_name in optional_deps, f"novafabric[{extra_name}] extra does not exist"
    deps = optional_deps[extra_name]
    assert any(dep.split(">=")[0].split("==")[0].strip() == expected_package for dep in deps), (
        f"novafabric[{extra_name}] does not depend on {expected_package!r}: {deps!r}"
    )


def test_all_extra_exists_and_is_self_referencing() -> None:
    optional_deps = _load_pyproject()["project"]["optional-dependencies"]
    assert "all" in optional_deps
    all_deps = optional_deps["all"]
    assert len(all_deps) == 1
    assert all_deps[0].startswith("novafabric[")


def test_all_extra_pulls_in_the_new_narrow_extras() -> None:
    optional_deps = _load_pyproject()["project"]["optional-dependencies"]
    all_spec = optional_deps["all"][0]
    referenced = set(re.search(r"novafabric\[(.*)\]", all_spec).group(1).split(","))
    for extra in ("clickhouse", "nats", "avro", "energy-gpu"):
        assert extra in referenced, f"novafabric[all] does not include [{extra}]"


def test_all_extra_excludes_cloud_vendor_and_agent_framework_extras() -> None:
    """The escape-hatch `all` extra must stay narrow on vendor/framework choice.

    Bundling every cloud vendor's SDK (or every competing agent framework's
    adapter) into the "just install everything" extra would make `all`
    install dead weight for every single user, since nobody uses more than
    one cloud vendor or agent framework at a time.
    """
    optional_deps = _load_pyproject()["project"]["optional-dependencies"]
    all_spec = optional_deps["all"][0]
    referenced = set(re.search(r"novafabric\[(.*)\]", all_spec).group(1).split(","))

    excluded = _CLOUD_VENDOR_EXTRAS | _AGENT_FRAMEWORK_EXTRAS
    leaked = referenced & excluded
    assert not leaked, f"novafabric[all] must not reference {leaked!r}"

    # And sanity-check the exclusion sets themselves are still real extras
    # (guards against the excluded-extra itself being renamed/removed without
    # updating this test).
    for extra in excluded:
        assert extra in optional_deps, f"expected extra {extra!r} no longer exists"


def test_clickhouse_connect_no_longer_in_core_dependencies() -> None:
    """S2 / ADR-0222 completed the move this test used to hold the line against.

    Previously this asserted the opposite — that S1 must NOT remove
    clickhouse-connect from ``[project.dependencies]``, because that move
    belonged to the later S2 slice. S2 has now landed, so the assertion is
    inverted: the dependency must stay out of core, reachable only via the
    ``clickhouse`` (or wider ``scale``) extra.

    The full lean-install contract is covered by
    ``tests/packaging_metadata/test_lean_install_surface.py``; this is the narrow
    hand-off marker between the two slices.
    """
    deps = _project_table()["dependencies"]
    assert not any(d.startswith("clickhouse-connect") for d in deps), (
        "clickhouse-connect is back in [project.dependencies] — ADR-0222 moved "
        "it to the [clickhouse] and [scale] extras"
    )
    optional_deps = _load_pyproject()["project"]["optional-dependencies"]
    for extra in ("clickhouse", "scale"):
        assert any(d.startswith("clickhouse-connect") for d in optional_deps[extra]), (
            f"[{extra}] must still pin clickhouse-connect"
        )
