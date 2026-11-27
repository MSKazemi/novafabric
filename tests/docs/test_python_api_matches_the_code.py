"""``docs/python-api.md`` must describe the Python API that actually imports.

The page is written by hand; nothing generated it from the code, so it drifted:
an audit on 2026-11-27 found missing parameters (``CaptureOrchestrator.run``,
``ReplayFlags.hermetic``, ``NovaSeal(tsa_urls=, backend=)``), result fields the
page never listed, and 65 of the 140 names exported through the ``__all__`` of
the modules it documents that the page did not mention. This guard keeps the two in step mechanically:

* every ``from novafabric... import X`` in a code block on the page imports;
* every dotted ``novafabric.x.y`` reference in inline code resolves to a module
  or an attribute;
* every name in the ``__all__`` of a module the page documents appears in the
  page's code (a fenced block or an inline code span). The top-level package is
  in that list: it has no ``__all__`` today, so the check starts covering it the
  day one is added.

Signatures and behaviour are still reviewed by hand; this catches the class of
drift a reader hits first, a name that does not import or is not documented.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
PAGE = REPO / "docs" / "python-api.md"

#: Modules whose whole ``__all__`` the page documents. A new section on the page
#: whose import root has an ``__all__`` belongs here.
DOCUMENTED_MODULES = (
    "novafabric",
    "novafabric.sdk",
    "novafabric.capture",
    "novafabric.capture.record",
    "novafabric.replay",
    "novafabric.diff",
    "novafabric.lineage",
    "novafabric.lineage.analytics",
    "novafabric.spec",
    "novafabric.trust.novaseal",
    "novafabric.scores",
    "novafabric.client",
    "novafabric.compliance.export.provenance",
)

#: Dotted names on the page that are not Python objects.
NOT_PYTHON_OBJECTS = frozenset(
    {
        "novafabric.hooks",  # an entry-point group name, not a module
    }
)

_FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.M | re.S)
_INLINE = re.compile(r"`([^`\n]+)`")
_FROM_IMPORT = re.compile(r"^\s*from\s+(novafabric[\w.]*)\s+import\s+(\([^)]*\)|[^\n]+)", re.M)
_IMPORT = re.compile(r"^\s*import\s+(novafabric[\w.]*)", re.M)
# Not preceded by a word character or a dot, so ``io.novafabric.x`` (an
# extension key) and ``~/.novafabric/`` (a path) are not taken for imports.
_DOTTED = re.compile(r"(?<![\w.])novafabric(?:\.[A-Za-z_]\w*)+")
_IDENT = re.compile(r"[A-Za-z_]\w*")


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def _fences(text: str) -> list[str]:
    return _FENCE.findall(text)


def _inline_spans(text: str) -> list[str]:
    return _INLINE.findall(_FENCE.sub("", text))


def _imported_names(text: str) -> list[tuple[str, str]]:
    """``(module, name)`` for every ``from novafabric... import name`` in a code block."""
    pairs: list[tuple[str, str]] = []
    for block in _fences(text):
        for match in _FROM_IMPORT.finditer(block):
            module, clause = match.group(1), match.group(2)
            # Drop trailing comments line by line, then the parentheses.
            clause = "\n".join(line.split("#", 1)[0] for line in clause.splitlines())
            clause = clause.replace("(", " ").replace(")", " ")
            for item in clause.split(","):
                name = item.strip().split(" as ")[0].strip()
                if name:
                    pairs.append((module, name))
    return pairs


def _imported_modules(text: str) -> list[str]:
    return [m.group(1) for block in _fences(text) for m in _IMPORT.finditer(block)]


def _dotted_references(text: str) -> list[str]:
    refs = {ref for span in _inline_spans(text) for ref in _DOTTED.findall(span)}
    return sorted(refs - NOT_PYTHON_OBJECTS)


def _resolve(dotted: str) -> Any:
    """Import the longest module prefix of *dotted*, then walk the attributes."""
    parts = dotted.split(".")
    for cut in range(len(parts), 0, -1):
        try:
            obj: Any = importlib.import_module(".".join(parts[:cut]))
        except ModuleNotFoundError:
            continue
        for attr in parts[cut:]:
            obj = getattr(obj, attr)
        return obj
    raise ModuleNotFoundError(dotted)


def _import_from(module: str, name: str) -> Any:
    """What ``from module import name`` yields: an attribute or a submodule."""
    mod: ModuleType = importlib.import_module(module)
    if hasattr(mod, name):
        return getattr(mod, name)
    return importlib.import_module(f"{module}.{name}")


def _documented_identifiers(text: str) -> set[str]:
    code = "\n".join(_fences(text) + _inline_spans(text))
    return set(_IDENT.findall(code))


def test_the_page_has_code_to_check() -> None:
    """The parsers below found something: a format change cannot make them vacuous."""
    text = _page()
    assert len(_imported_names(text)) > 50, (
        "fewer imports than expected — did the page's code fences change?"
    )
    assert len(_dotted_references(text)) > 10
    assert len(_documented_identifiers(text)) > 300


def test_every_name_the_page_imports_is_importable() -> None:
    failures = []
    for module, name in _imported_names(_page()):
        try:
            _import_from(module, name)
        except (ImportError, AttributeError) as exc:
            failures.append(f"from {module} import {name}: {type(exc).__name__}: {exc}")
    for module in _imported_modules(_page()):
        try:
            importlib.import_module(module)
        except ImportError as exc:
            failures.append(f"import {module}: {exc}")
    assert not failures, "docs/python-api.md imports names that do not exist:\n" + "\n".join(
        failures
    )


def test_every_dotted_reference_resolves() -> None:
    failures = []
    for ref in _dotted_references(_page()):
        try:
            _resolve(ref)
        except (ImportError, AttributeError) as exc:
            failures.append(f"`{ref}`: {type(exc).__name__}: {exc}")
    assert not failures, (
        "docs/python-api.md names `novafabric.*` objects that do not resolve "
        "(add a real non-object name to NOT_PYTHON_OBJECTS):\n" + "\n".join(failures)
    )


@pytest.mark.parametrize("module_name", DOCUMENTED_MODULES)
def test_every_exported_name_is_documented(module_name: str) -> None:
    module = importlib.import_module(module_name)
    exported = list(getattr(module, "__all__", ()))
    documented = _documented_identifiers(_page())
    missing = [name for name in exported if name not in documented]
    assert not missing, (
        f"{module_name}.__all__ exports names docs/python-api.md does not mention "
        f"in code: {missing}. Add a short entry for each."
    )


@pytest.mark.parametrize("module_name", DOCUMENTED_MODULES[1:])
def test_every_documented_module_is_named_on_the_page(module_name: str) -> None:
    """The list above stays honest: each module it covers is one the page documents."""
    assert module_name in _page(), f"{module_name} is not named on docs/python-api.md"
