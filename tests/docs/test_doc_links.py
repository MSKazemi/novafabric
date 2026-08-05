"""Every relative link in a public markdown file must resolve.

Regression guard for a defect found on 2026-08-05: 142 markdown links across the
public documentation pointed into the private ``design/`` tree, which is excluded
from the public repository. Every one of them 404'd for every visitor — including
the RFC-process link that ``CONTRIBUTING.md`` instructs a new contributor to read
before anything else.

The suite runs from a working tree where ``design/`` *is* present, so existence
alone cannot catch this class. The checker rejects private-tree prefixes by name
for exactly that reason.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER = REPO_ROOT / "scripts" / "check_doc_links.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_doc_links", CHECKER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_doc_links"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker():
    if not CHECKER.exists():  # pragma: no cover - only in a partial checkout
        pytest.skip(f"{CHECKER} not present")
    return _load_checker()


def test_no_broken_relative_links_in_public_docs(checker) -> None:
    tracked = checker.tracked_paths()
    broken = []
    for path in checker.tracked_markdown():
        if path.exists():
            broken.extend(checker.check_file(path, tracked))

    assert not broken, "broken documentation links:\n" + "\n".join(
        f"  {item.render()}" for item in broken
    )


def test_checker_rejects_a_link_to_a_file_that_exists_but_is_not_public(
    checker, tmp_path, monkeypatch
) -> None:
    """The core rule: existence is not the test, public-git membership is.

    Files like ``THREAT_MODEL.md`` and ``CLAUDE.md`` are present in the working
    tree and excluded from the public git. An existence check passes on a
    maintainer's machine and fails only for the reader — which is exactly how
    nine such links survived the first, prefix-denylist version of this gate.
    """
    monkeypatch.setattr(checker, "REPO_ROOT", tmp_path)

    private = tmp_path / "THREAT_MODEL.md"
    private.write_text("private\n", encoding="utf-8")
    public = tmp_path / "SECURITY.md"
    public.write_text("public\n", encoding="utf-8")

    doc = tmp_path / "page.md"
    doc.write_text("[threat model](THREAT_MODEL.md) [security](SECURITY.md)\n", encoding="utf-8")

    broken = checker.check_file(doc, frozenset({"SECURITY.md", "page.md"}))

    assert [item.target for item in broken] == ["THREAT_MODEL.md"]
    assert "NOT tracked" in broken[0].reason
    assert private.exists(), "the rejected target really does exist on disk"


def test_checker_accepts_a_link_to_a_tracked_directory(checker, tmp_path, monkeypatch) -> None:
    """`docs/rfcs/` is how a reader browses on GitHub; git tracks only files."""
    monkeypatch.setattr(checker, "REPO_ROOT", tmp_path)

    (tmp_path / "docs" / "rfcs").mkdir(parents=True)
    (tmp_path / "docs" / "rfcs" / "README.md").write_text("hi\n", encoding="utf-8")
    doc = tmp_path / "page.md"
    doc.write_text("[rfcs](docs/rfcs/)\n", encoding="utf-8")

    tracked = frozenset({"docs/rfcs/README.md", "docs/rfcs", "docs", "page.md"})

    assert checker.check_file(doc, tracked) == []


def test_checker_rejects_a_target_that_does_not_exist(checker, tmp_path) -> None:
    doc = tmp_path / "page.md"
    doc.write_text("[missing](./nowhere.md)\n", encoding="utf-8")

    broken = checker.check_file(doc)

    assert len(broken) == 1
    assert "does not exist" in broken[0].reason


def test_checker_ignores_external_links_and_bare_anchors(checker, tmp_path) -> None:
    doc = tmp_path / "page.md"
    doc.write_text(
        "[web](https://example.com) [mail](mailto:a@b.c) [anchor](#section)\n",
        encoding="utf-8",
    )

    assert checker.check_file(doc) == []


def test_checker_ignores_links_inside_fenced_code_blocks(checker, tmp_path) -> None:
    """A link in an example is documentation of a link, not a link."""
    doc = tmp_path / "page.md"
    doc.write_text(
        "```markdown\n[example](design/adr/0001-nope.md)\n```\n",
        encoding="utf-8",
    )

    assert checker.check_file(doc) == []
