"""Guard: every schema the runtime loads must exist in an installed wheel.

Three shipped features were broken on every `pip install novafabric`, and the
test suite could not see any of them, because the suite runs from the source
tree where the repo-root fallback resolves:

- `novafabric.envelope.validator` raised
  `FileNotFoundError: envelope-v1.json not found` — Event Envelope v1
  validation was simply unavailable.
- `novafabric.import_blob` resolved its manifest schema to a path *outside*
  site-packages, so batch import could not validate a manifest.
- `novafabric.capsule.validator.validate_capsule_json` raised FileNotFoundError
  — parent/child capsule validation was unavailable. This third one was found
  by the class-level guard below, not by hand, which is the argument for it.

Nothing under the repo-root `schemas/` ships in the wheel; only
`src/novafabric/**` and whatever `force-include` maps in. These tests pin both
halves of the contract — the code prefers a packaged path, and `pyproject.toml`
actually ships something to that path — without building a wheel, so they are
fast enough to run on every commit.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "novafabric"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _wheel_config() -> dict[str, object]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["tool"]["hatch"]["build"]["targets"]["wheel"]  # type: ignore[no-any-return,index]


def _force_include() -> dict[str, str]:
    return dict(_wheel_config().get("force-include", {}))  # type: ignore[arg-type]


def _include_globs() -> list[str]:
    return list(_wheel_config().get("include", []))  # type: ignore[arg-type]


def _ships_to(target: str) -> bool:
    """Would a file land at `target` (a path relative to the wheel root)?"""
    if target in _force_include().values():
        return True
    relative = target.removeprefix("novafabric/")
    for glob in _include_globs():
        if Path(glob.removeprefix("src/novafabric/")).match(relative) or Path(
            "src/novafabric/" + relative
        ).match(glob):
            return True
    return False


# --------------------------------------------------------------------------
# The two paths that were broken
# --------------------------------------------------------------------------


def test_envelope_schema_is_packaged_and_preferred() -> None:
    from novafabric.envelope import validator

    packaged = validator._PACKAGED_SCHEMA_PATH
    assert packaged.parent.name == "_schemas"
    assert SRC in packaged.parents, "packaged path must live inside the package"

    target = "novafabric/envelope/_schemas/envelope-v1.json"
    assert _ships_to(target), f"nothing ships to {target}; a wheel would 404 here"

    # And the code must try the packaged path FIRST — in a wheel it is the only
    # candidate that exists, so ordering is the whole fix.
    source = (SRC / "envelope" / "validator.py").read_text(encoding="utf-8")
    body = source.split("def _locate_schema()", 1)[1]
    assert body.index("_PACKAGED_SCHEMA_PATH") < body.index("_SCHEMA_PATH,")


def test_export_manifest_schema_is_packaged_and_preferred() -> None:
    from novafabric.import_blob import service

    packaged = service.Path(service.__file__).resolve().parents[1] / "schemas"
    assert packaged.is_dir()

    target = "novafabric/schemas/export-manifest.schema.json"
    assert _ships_to(target), f"nothing ships to {target}; a wheel would 404 here"

    source = (SRC / "import_blob" / "service.py").read_text(encoding="utf-8")
    assert "_locate_export_manifest_schema" in source
    body = source.split("def _locate_export_manifest_schema", 1)[1]
    assert body.index("packaged") < body.index("parents[3]"), "packaged path must be tried first"


def test_force_include_sources_exist() -> None:
    """A force-include naming a missing file fails the build, not the tests."""
    for source in _force_include():
        assert (REPO_ROOT / source).exists(), f"force-include source missing: {source}"


def test_forced_schemas_are_the_canonical_files_not_copies() -> None:
    """ADR-0028's packaged-vs-canonical split cost a release. There must be
    exactly ONE copy of each of these schemas in the repo — the build maps it
    into the package, so the two can never drift."""
    for source, target in _force_include().items():
        if not target.endswith(".json"):
            continue
        duplicate = SRC / Path(target).relative_to("novafabric")
        assert not duplicate.exists(), (
            f"{duplicate} is a second copy of {source}; force-include already "
            "ships the canonical file, so this one will drift"
        )


# --------------------------------------------------------------------------
# The class, not just the two instances
# --------------------------------------------------------------------------


def _modules_escaping_to_repo_root() -> dict[Path, int]:
    """Modules that build a path with `parents[N]` / `.parent` chains deep
    enough to leave `src/novafabric/`, combined with a "schemas" segment.

    `src/novafabric/<pkg>/<mod>.py` is 2 levels below the package root, so any
    reference reaching 3+ levels up has left the wheel.
    """
    offenders: dict[Path, int] = {}
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if '"schemas"' not in text and "'schemas'" not in text:
            continue
        tree = ast.parse(text)
        depth = 0
        for node in ast.walk(tree):
            # parents[N]
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "parents"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, int)
            ):
                depth = max(depth, node.slice.value)
            # .parent.parent.parent...
            if isinstance(node, ast.Attribute) and node.attr == "parent":
                chain, cursor = 0, node
                while isinstance(cursor, ast.Attribute) and cursor.attr == "parent":
                    chain += 1
                    cursor = cursor.value  # type: ignore[assignment]
                depth = max(depth, chain)
        if depth >= 3:
            offenders[path.relative_to(REPO_ROOT)] = depth
    return offenders


#: Modules known to reach outside the package, each with a packaged path tried
#: first. Adding to this list means you have checked the wheel really ships it.
_ALLOWED_ESCAPES = {
    Path("src/novafabric/envelope/validator.py"),
    Path("src/novafabric/import_blob/service.py"),
    Path("src/novafabric/capsule/validator.py"),
}


def test_no_new_module_loads_a_schema_from_outside_the_package() -> None:
    """The generalisation of this bug: a repo-root schema path is invisible in
    the source tree and fatal in a wheel. New ones must be justified here."""
    offenders = _modules_escaping_to_repo_root()
    unexpected = {p: d for p, d in offenders.items() if p not in _ALLOWED_ESCAPES}
    assert not unexpected, (
        "these modules resolve a 'schemas' path outside src/novafabric, which "
        "does not exist in an installed wheel:\n"
        + "\n".join(f"  {p} (reaches {d} levels up)" for p, d in sorted(unexpected.items()))
        + "\nAdd a packaged path (tried first) + a force-include mapping, then "
        "allow-list it in _ALLOWED_ESCAPES."
    )


@pytest.mark.parametrize("module", sorted(_ALLOWED_ESCAPES, key=str))
def test_allowed_escapes_are_still_real(module: Path) -> None:
    """Keeps the allow-list honest: an entry whose module no longer escapes is
    stale and would hide a future regression behind a name that looks handled."""
    assert (REPO_ROOT / module).exists(), f"allow-listed module no longer exists: {module}"
    assert module in _modules_escaping_to_repo_root(), (
        f"{module} no longer resolves a repo-root schema path — remove it from "
        "_ALLOWED_ESCAPES so the guard stays meaningful"
    )
