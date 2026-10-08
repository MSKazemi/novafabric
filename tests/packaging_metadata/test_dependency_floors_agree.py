"""A dependency declared in several places carries one identical specifier.

Why this exists: Dependabot's ``uv`` updater could not update ``boto3`` at all.
The private-repo run 37847811489 (2026-10-08) died with ``Declaration not found
for boto3!`` raised from ``RequirementReplacer#original_dependency_declaration_
string``. At the time ``boto3`` was declared five times across the extras and the
``dev`` group under four different floor strings (``>=1.35.0``, ``>=1.38``,
``>=1.38.0``, ``>=1.43.10``). Dependabot folds a package's declarations into one
requirement and then looks for that requirement *string* in ``pyproject.toml`` to
rewrite it; with differing strings it finds no match and drops the update, so the
package silently stops receiving update PRs.

The invariant is checked over every package rather than ``boto3`` alone, so the
same failure cannot recur for ``botocore``, ``aioboto3`` or anything added later.
A second check pins each shared floor at or below the version in ``uv.lock``:
harmonising must never raise a floor above what the lock actually resolves.
"""

from __future__ import annotations

import tomllib
from collections import defaultdict
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_LOCK = _REPO_ROOT / "uv.lock"

_SELF = "novafabric"


def _declarations(pyproject: dict) -> dict[str, list[tuple[str, Requirement]]]:  # type: ignore[type-arg]
    """``name -> [(where, requirement), ...]`` over dependencies, extras, groups."""
    out: dict[str, list[tuple[str, Requirement]]] = defaultdict(list)

    def add(where: str, items: list[object]) -> None:
        for item in items:
            if not isinstance(item, str):  # {include-group = ...} tables
                continue
            req = Requirement(item)
            if req.name.lower() == _SELF:
                continue
            out[req.name.lower()].append((where, req))

    project = pyproject["project"]
    add("[project.dependencies]", project.get("dependencies", []))
    for extra, items in project.get("optional-dependencies", {}).items():
        add(f"extra {extra!r}", items)
    for group, items in pyproject.get("dependency-groups", {}).items():
        add(f"group {group!r}", items)
    return out


def _disagreements(pyproject: dict) -> dict[str, list[str]]:  # type: ignore[type-arg]
    return {
        name: sorted(f"{where}: {req}" for where, req in decls)
        for name, decls in _declarations(pyproject).items()
        if len({str(req.specifier) for _, req in decls}) > 1
    }


def _locked_versions() -> dict[str, Version]:
    lock = tomllib.loads(_LOCK.read_text(encoding="utf-8"))
    return {pkg["name"].lower(): Version(pkg["version"]) for pkg in lock["package"] if "version" in pkg}


def test_every_multiply_declared_dependency_uses_one_specifier() -> None:
    pyproject = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    assert _disagreements(pyproject) == {}, (
        "a dependency is declared with different specifiers in different "
        "extras/groups; Dependabot then fails with 'Declaration not found' and "
        "never updates it. Use one identical specifier string everywhere."
    )


def test_the_boto_family_is_covered() -> None:
    """boto3 is the case that broke; it must stay under the invariant."""
    pyproject = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    decls = _declarations(pyproject)
    assert len(decls["boto3"]) >= 2, "boto3 is no longer multiply declared; revisit this guard"
    for name in ("boto3", "botocore", "aioboto3", "aiobotocore"):
        assert len({str(req.specifier) for _, req in decls.get(name, [])}) <= 1, name


def test_shared_floors_do_not_exceed_the_locked_version() -> None:
    pyproject = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    locked = _locked_versions()
    for name, decls in _declarations(pyproject).items():
        if len(decls) < 2 or name not in locked:
            continue
        for where, req in decls:
            assert req.specifier.contains(locked[name], prereleases=True), (
                f"{where}: {req} excludes the locked {name}=={locked[name]}"
            )


def test_the_guard_detects_differing_floors() -> None:
    """Red check: the exact pre-fix shape is reported, with every location."""
    broken = {
        "project": {
            "dependencies": [],
            "optional-dependencies": {
                "scale": ["boto3>=1.35.0"],
                "worm-s3": ["boto3>=1.38"],
                "seal-aws": ["boto3>=1.38.0"],
            },
        },
        "dependency-groups": {"dev": ["boto3>=1.38", {"include-group": "x"}]},
    }
    found = _disagreements(broken)
    assert set(found) == {"boto3"}
    assert len(found["boto3"]) == 4


def test_the_guard_accepts_one_shared_floor() -> None:
    ok = {
        "project": {
            "optional-dependencies": {"a": ["boto3>=1.43.10"], "b": ["boto3>=1.43.10"]},
        },
    }
    assert _disagreements(ok) == {}
