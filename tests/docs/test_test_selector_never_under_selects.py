"""The test selector must fail SAFE — ambiguity selects more, never less.

`scripts/testsel.py` decides which tests the automated tiers run (ADR-0267). If it
silently under-selects, every tier above it reports green on a change it never
exercised, which is worse than having no selector: this repository has already lost
73 Docker-free tests to a scoping shortcut that looked correct.

So these tests are mostly about the *escalation* paths, not the happy path.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SELECTOR = REPO / "scripts" / "testsel.py"


@pytest.fixture(scope="module")
def testsel():
    spec = importlib.util.spec_from_file_location("_testsel", SELECTOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def index(testsel):
    return testsel.build_index()


def test_the_selector_exists_and_is_executable() -> None:
    assert SELECTOR.is_file(), "the automated test tiers depend on this script"


def test_index_covers_a_meaningful_number_of_test_files(index) -> None:
    """A near-empty index would make every selection vacuously small."""
    assert len(index) > 500, f"index has only {len(index)} entries — selection would be vacuous"


@pytest.mark.parametrize(
    "path",
    ["pyproject.toml", "uv.lock", "Makefile", "tests/conftest.py"],
)
def test_a_global_trigger_escalates_to_the_whole_suite(testsel, index, path: str) -> None:
    """These can affect anything, so scoping them at all would be a lie."""
    selected, reason = testsel.select([path], index)
    assert selected == [testsel.SENTINEL_ALL], f"{path} must escalate, got {reason}"


def test_python_outside_src_and_tests_escalates(testsel, index) -> None:
    """The selector has no dependency model for it, so it must not guess."""
    selected, _ = testsel.select(["scripts/testsel.py"], index)
    assert selected == [testsel.SENTINEL_ALL]


def test_a_source_change_selects_tests_that_import_it(testsel, index) -> None:
    selected, _ = testsel.select(["src/novafabric/lineage/backends/kuzu.py"], index)
    assert selected != []
    assert "tests/lineage/test_backends_kuzu.py" in selected


def test_a_changed_test_file_selects_itself(testsel, index) -> None:
    target = "tests/lineage/test_backends_kuzu.py"
    selected, _ = testsel.select([target], index)
    assert selected == [target]


def test_a_changed_conftest_selects_its_whole_subtree(testsel, index) -> None:
    selected, _ = testsel.select(["tests/lineage/conftest.py"], index)
    assert all(t.startswith("tests/lineage/") for t in selected)
    assert len(selected) > 1, "a conftest governs more than one file"


def test_a_non_python_change_does_not_suppress_another_paths_selection(
    testsel, index
) -> None:
    """A docs edit alongside a code edit must not shrink the code edit's selection."""
    code_only, _ = testsel.select(["src/novafabric/lineage/backends/kuzu.py"], index)
    with_doc, _ = testsel.select(
        ["docs/lineage/migration-guide.md", "src/novafabric/lineage/backends/kuzu.py"],
        index,
    )
    assert set(with_doc) == set(code_only)


def test_direct_mode_finds_the_obvious_tests_for_a_module(testsel) -> None:
    selected, _ = testsel.select_direct(["src/novafabric/lineage/backends/kuzu.py"])
    assert "tests/lineage/test_backends_kuzu.py" in selected


def test_direct_mode_ignores_paths_it_cannot_map(testsel) -> None:
    """Tier 0 is narrow by design; it must return nothing rather than guess."""
    selected, _ = testsel.select_direct(["README.md"])
    assert selected == []


def test_no_changed_files_selects_nothing(testsel, index) -> None:
    selected, _ = testsel.select([], index)
    assert selected == []
