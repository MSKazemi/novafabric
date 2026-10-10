"""The PyPI long description must carry no repository-relative links.

pypi.org renders README.md with no repository root, so ``docs/x.md`` and the demo
image break (84 links at the time of writing). ``scripts/pypi_readme_hook.py`` rewrites them at
build time to absolute URLs pinned to ``v{version}``. These tests run the rewrite on
the REAL README and assert nothing relative is left, using a detector independent of
the rewriter's own regexes, plus a non-vacuity self-test.
"""

from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path
from types import ModuleType

_ROOT = Path(__file__).resolve().parents[2]
_VERSION = "9.9.9"


def _hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "pypi_readme_hook", _ROOT / "scripts" / "pypi_readme_hook.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_TARGET = re.compile(
    r"""\]\(\s*<?([^)\s>]+)|\bsrc\s*=\s*["']([^"']*)|\bhref\s*=\s*["']([^"']*)""",
    re.IGNORECASE,
)


def _relative_targets(markdown: str) -> list[str]:
    """Independent detector: every link/img target that is not absolute or `#`."""
    # Drop fenced code (examples there are not links).
    prose = re.sub(r"^```.*?^```", "", markdown, flags=re.MULTILINE | re.DOTALL)
    found = [next(g for g in m.groups() if g is not None) for m in _TARGET.finditer(prose)]
    return [t for t in found if t and not re.match(r"(https?://|mailto:|#)", t)]


def test_real_readme_has_relative_links_to_rewrite() -> None:
    """Non-vacuity: the unrewritten README really contains relative links."""
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert len(_relative_targets(readme)) >= 20


def test_identity_rewrite_would_be_caught() -> None:
    """Non-vacuity: the detector fails on an un-rewritten README."""
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert _relative_targets(readme), "detector must flag the raw README"


def test_rewritten_readme_has_no_relative_links() -> None:
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    out = _hook().rewrite_readme(readme, _VERSION)
    assert _relative_targets(out) == []
    assert f"/blob/v{_VERSION}/docs/" in out
    assert (
        f"https://raw.githubusercontent.com/MSKazemi/novafabric/v{_VERSION}/docs/assets/demo.svg"
        in out
    )


def test_rewrite_is_idempotent_and_leaves_code_fences_alone() -> None:
    h = _hook()
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    once = h.rewrite_readme(readme, _VERSION)
    assert h.rewrite_readme(once, _VERSION) == once
    fenced = "```\n[x](docs/a.md)\n```\n"
    assert h.rewrite_readme(fenced, _VERSION) == fenced


def test_link_shapes() -> None:
    a = _hook().absolutize
    blob = f"https://github.com/MSKazemi/novafabric/blob/v{_VERSION}/"
    raw = f"https://raw.githubusercontent.com/MSKazemi/novafabric/v{_VERSION}/"
    assert a("docs/x.md#sec", _VERSION) == blob + "docs/x.md#sec"
    assert a("./docs/x.md", _VERSION) == blob + "docs/x.md"
    assert a("docs/a/shot.PNG", _VERSION) == raw + "docs/a/shot.PNG"
    assert a("examples/", _VERSION) == blob.replace("/blob/", "/tree/") + "examples/"
    for untouched in ("#anchor", "https://novafabric.ai/docs/", "mailto:a@b.c", "//cdn.x/y.js"):
        assert a(untouched, _VERSION) == untouched


def test_all_syntaxes_are_rewritten() -> None:
    h = _hook()
    src = (
        '[a](docs/a.md "t") ![i](docs/i.svg) [![b](docs/b.png)](docs/c.md)\n'
        '<img src="docs/i.svg" width="1"> <a href=\'docs/a.md\'>x</a>\n'
        "[ref]: docs/r.md\n[same](#top) [abs](https://e.com/x)\n"
    )
    out = h.rewrite_readme(src, _VERSION)
    assert _relative_targets(out) == []
    assert "[same](#top)" in out and "https://e.com/x" in out


def test_pyproject_wires_the_hook_and_readme_is_dynamic() -> None:
    cfg = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "readme" in cfg["project"]["dynamic"]
    assert "readme" not in cfg["project"]
    assert (
        cfg["tool"]["hatch"]["metadata"]["hooks"]["custom"]["path"] == "scripts/pypi_readme_hook.py"
    )
