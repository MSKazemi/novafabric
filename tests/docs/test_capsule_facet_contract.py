"""Every capsule facet module honours the same contract.

Adding a facet is the most-repeated extension pattern in this codebase — 22 modules
at the time of writing — and `docs/developer-guide.md` documents it as a recipe. A
documented recipe that nothing enforces becomes false the first time someone
deviates, so this walks the tree rather than naming modules.

⚠ **What this deliberately does NOT require: `facet_from_capsule`.** 15 of the 22
have a reader and 7 do not, and that is a real distinction, not a backlog: nothing
reads those 7 back today (checked 2026-09-02 — no module outside each facet's own
file references `facets["<name>"]`). Requiring a reader here would force seven new
functions with zero callers, which is the `bind_recorder` mistake this repository
has already made once. A reader is added when something needs to read.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "novafabric"

#: Symbols every facet module must define. Measured, not aspirational — all 22
#: modules already satisfy this, which is what makes it a contract rather than a wish.
REQUIRED = ("FACET_NAME", "SCHEMA_VERSION")
REQUIRED_FUNCTIONS = ("attach_facet",)


def _module_symbols(path: pathlib.Path) -> tuple[set[str], set[str]]:
    """Top-level assigned names and function names in *path*."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), str(path))
    names: set[str] = set()
    functions: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names.update(
                t.id for t in node.targets if isinstance(t, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.FunctionDef):
            functions.add(node.name)
    return names, functions


def _facet_modules() -> list[pathlib.Path]:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        names, _ = _module_symbols(path)
        if "FACET_NAME" in names:
            found.append(path)
    return found


FACET_MODULES = _facet_modules()


def test_the_guard_actually_finds_the_facet_modules() -> None:
    """A discovery bug would make every assertion below vacuously true."""
    assert len(FACET_MODULES) >= 20, (
        f"only {len(FACET_MODULES)} facet module(s) discovered — the walk is broken, "
        "and every parametrised check below would pass over an empty set"
    )


@pytest.mark.parametrize(
    "path", FACET_MODULES, ids=[str(p.relative_to(SRC)) for p in FACET_MODULES]
)
def test_a_facet_module_declares_its_name_and_schema_version(
    path: pathlib.Path,
) -> None:
    names, _ = _module_symbols(path)
    missing = [n for n in REQUIRED if n not in names]

    assert not missing, (
        f"{path.relative_to(SRC)} defines FACET_NAME but is missing {missing}. "
        "A facet without a SCHEMA_VERSION cannot be read back safely once its "
        "shape changes. See 'Adding a capsule facet' in docs/developer-guide.md."
    )


@pytest.mark.parametrize(
    "path", FACET_MODULES, ids=[str(p.relative_to(SRC)) for p in FACET_MODULES]
)
def test_a_facet_module_can_attach_itself_to_a_capsule(
    path: pathlib.Path,
) -> None:
    _, functions = _module_symbols(path)
    missing = [f for f in REQUIRED_FUNCTIONS if f not in functions]

    assert not missing, (
        f"{path.relative_to(SRC)} defines FACET_NAME but is missing {missing}. "
        "Every facet is written into the capsule the same way, additively. "
        "See 'Adding a capsule facet' in docs/developer-guide.md."
    )


def test_facet_names_are_unique() -> None:
    """Two modules writing the same key would silently overwrite each other."""
    seen: dict[str, str] = {}
    collisions: list[str] = []
    for path in FACET_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "FACET_NAME"
                    and isinstance(node.value, ast.Constant)
                ):
                    key = str(node.value.value)
                    rel = str(path.relative_to(SRC))
                    if key in seen:
                        collisions.append(f"{key!r}: {seen[key]} and {rel}")
                    seen[key] = rel

    assert not collisions, (
        "two facet modules claim the same facets[...] key, so one silently "
        f"overwrites the other: {collisions}"
    )
    assert len(seen) >= 20, f"only {len(seen)} facet names extracted"


def _declared_facet_names() -> dict[str, str]:
    """``FACET_NAME`` -> module, for every facet module in the tree."""
    declared: dict[str, str] = {}
    for path in FACET_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "FACET_NAME"
                    and isinstance(node.value, ast.Constant)
                ):
                    declared[str(node.value.value)] = str(path.relative_to(SRC))
    return declared


def _facet_registry(schema_path: pathlib.Path) -> dict[str, object]:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return dict(schema["properties"]["facets"]["properties"])


#: The three live copies of the capsule schema. The packaged one is what an installed
#: CLI validates against; ``schemas/`` is the not-yet-in-force OAS v1.0 target
#: (ADR-0034 §1); ``web/src/data/schemas/`` is the dashboard's copy. All three close
#: the facets registry, so all three must know every facet name.
SCHEMA_COPIES = (
    SRC / "schemas" / "run-capsule.schema.json",
    SRC.parents[1] / "schemas" / "run-capsule.schema.json",
    SRC.parents[1] / "web" / "src" / "data" / "schemas" / "run-capsule.schema.json",
)


@pytest.mark.parametrize(
    "schema_path", SCHEMA_COPIES, ids=[p.parent.parent.name for p in SCHEMA_COPIES]
)
def test_every_declared_facet_is_in_the_closed_registry(
    schema_path: pathlib.Path,
) -> None:
    """A facet name the registry does not know makes the capsule fail validation.

    ``facets`` is ``additionalProperties: false`` on purpose (ADR-0196 D2), so a module
    that writes ``facets["something_new"]`` produces a capsule that ``nova validate``
    rejects with *"Additional properties are not allowed"*. Both halves had passing
    tests before this one existed: each facet module tested its own ``attach_facet``
    against a plain dict, and the schema tested its own validity — and nothing spanned
    the two. On 2026-09-03 that gap was holding **13** unregistered facet names, 11 of
    them from features whose own tests were green.
    """
    declared = _declared_facet_names()
    registry = _facet_registry(schema_path)
    unregistered = sorted(name for name in declared if name not in registry)

    assert not unregistered, (
        "these modules write a facets[...] key that "
        f"{schema_path.name} does not register, so any capsule carrying one fails "
        "validation with 'Additional properties are not allowed':\n"
        + "\n".join(f"  {n!r} — {declared[n]}" for n in unregistered)
        + "\nRegister each name under properties.facets.properties in all three "
        "schema copies (see SCHEMA_COPIES in this file)."
    )


@pytest.mark.parametrize(
    "schema_path", SCHEMA_COPIES, ids=[p.parent.parent.name for p in SCHEMA_COPIES]
)
def test_the_registry_check_is_not_vacuous(schema_path: pathlib.Path) -> None:
    """Both sides must be non-empty, or the check above passes over nothing."""
    declared = _declared_facet_names()
    registry = _facet_registry(schema_path)
    assert len(declared) >= 20, f"only {len(declared)} facet names declared"
    assert len(registry) >= 20, f"only {len(registry)} names in {schema_path.name}"
    assert declared, "no declared names — the check above would be vacuous"
