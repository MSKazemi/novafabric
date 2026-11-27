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


def test_a_changed_test_helper_selects_the_tests_that_use_it_not_itself(testsel, index) -> None:
    """A helper is not a test file: pytest given `_harness.py` collects nothing (or
    crashes on an import-time environment read) while the tests importing it go unrun."""
    helper = "tests/replay/fidelity/_harness.py"
    selected, _ = testsel.select([helper], index)
    assert helper not in selected
    assert "tests/replay/fidelity/test_fidelity_corpus.py" in selected
    assert "tests/replay/fidelity/test_fidelity_controls.py" in selected


def test_a_test_helper_is_found_by_every_kind_of_importer(testsel, tmp_path, monkeypatch) -> None:
    """Subtree selection alone misses users elsewhere; a text match misses most forms."""
    files = {
        "pkg/__init__.py": "",
        "pkg/sub/__init__.py": "",
        "pkg/sub/_h.py": "X = 1\n",
        "other/test_dotted.py": "from pkg.sub._h import X\n",
        "other/test_from_parent.py": "from pkg.sub import _h\n",
        "other/test_multiline.py": "from pkg.sub import (\n    other_name,\n    _h,\n)\n",
        "other/test_comma.py": "import os, pkg.sub._h\n",
        "other/test_tests_prefix.py": "from tests.pkg.sub._h import X\n",
        "pkg/side/test_relative.py": "from ..sub._h import X\n",
        "other/_h2.py": "from pkg.sub._h import X\n",
        "other/test_transitive.py": "from other._h2 import X\n",
        "covered/conftest.py": "from pkg.sub._h import X\n",
        "covered/test_under_importing_conftest.py": "def test_x():\n    pass\n",
        "other/test_unrelated.py": "from pkg.sub import _hx\n",
    }
    for rel, body in files.items():
        path = tmp_path / "tests" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    monkeypatch.setattr(testsel, "REPO", tmp_path)
    fake_index = {f"tests/{rel}": [] for rel in files if rel.rsplit("/", 1)[-1].startswith("test_")}
    selected, _ = testsel.select(["tests/pkg/sub/_h.py"], fake_index)
    assert set(selected) == {
        "tests/other/test_dotted.py",
        "tests/other/test_from_parent.py",
        "tests/other/test_multiline.py",
        "tests/other/test_comma.py",
        "tests/other/test_tests_prefix.py",
        "tests/pkg/side/test_relative.py",
        "tests/other/test_transitive.py",
        "tests/covered/test_under_importing_conftest.py",
    }


def test_a_conftest_imported_by_another_conftest_selects_both_subtrees(testsel, index) -> None:
    """`tests/trend/conftest.py` imports `query.conftest`, so trend tests use it too."""
    selected, _ = testsel.select(["tests/query/conftest.py"], index)
    assert any(t.startswith("tests/trend/test_") for t in selected)


def test_a_non_python_file_named_like_a_test_is_not_selected(testsel, index) -> None:
    selected, _ = testsel.select(["tests/fixtures/test_data.json"], index)
    assert "tests/fixtures/test_data.json" not in selected


def test_the_gate_never_hands_pytest_a_helper_module(testsel, index) -> None:
    tier, tests, _ = testsel.select_gate(["tests/replay/fidelity/_workload.py"], index=index)
    assert tier == "scoped"
    assert "tests/replay/fidelity/test_fidelity_corpus.py" in tests
    assert all(pathlib.Path(t).name.startswith("test_") for t in tests), tests
