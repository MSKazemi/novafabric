# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Guard: every test package must be importable without basename collisions.

pytest imports a test module by its bare basename unless its directory is a
package. Two files with the same name in two non-package directories collide
and break **collection** — not one test, the whole run.

This is not hypothetical. `tests/embodied/` and `tests/federation/` were
written in parallel and both shipped a `test_facet.py`; the suite failed to
collect at all the moment they merged. Twenty-one test directories were
missing `__init__.py` at that point, so the same trap was armed in each of
them and had simply not been sprung yet.

A collision is invisible until two names happen to coincide, which makes it
exactly the kind of thing to assert rather than remember.
"""

from __future__ import annotations

import importlib.metadata
from collections import defaultdict
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parents[1]

#: Directories that hold data, not importable tests.
_NON_PACKAGE = ("fixtures", "__pycache__", ".pytest_cache")

#: Test packages that knowingly shadow an installed distribution.
#:
#: Empty, and it must stay that way. The last two entries — ``tests/a2a`` and
#: ``tests/mcp`` — were renamed to ``tests/a2a_adapter`` and
#: ``tests/mcp_conformance`` once the behavioural flip they were hiding was
#: verified: while they existed, ``mcp.client.session`` and ``a2a.client`` were
#: unimportable under pytest, so the guarded imports in
#: ``capture/hooks/_mcp.py`` and ``adapters/a2a.py`` silently took their
#: "library not installed" branch for every test run. Grandfathering a shadow
#: does not make it safe; it only postpones finding out what it hid.
_GRANDFATHERED_SHADOWS: frozenset[str] = frozenset()


def _test_dirs() -> list[Path]:
    """Every directory under tests/ that contains at least one test module."""
    return sorted(
        {
            path.parent
            for path in TESTS_ROOT.rglob("test_*.py")
            if not any(part in _NON_PACKAGE for part in path.parts)
            and path.parent != TESTS_ROOT
        }
    )


def test_every_test_directory_is_a_package() -> None:
    """A test dir without __init__.py is a collision waiting for a coincidence."""
    missing = [
        str(d.relative_to(TESTS_ROOT)) for d in _test_dirs() if not (d / "__init__.py").is_file()
    ]
    assert not missing, (
        "these test directories lack __init__.py, so their modules are imported "
        "by bare basename and will break collection the moment two filenames "
        f"coincide: {missing}"
    )


def test_no_basename_collision_outside_packages() -> None:
    """The failure mode itself, asserted directly.

    Belt and braces with the test above: package markers prevent the
    collision, but this catches it even if the marker rule is ever relaxed.
    """
    by_name: dict[str, list[str]] = defaultdict(list)
    for path in TESTS_ROOT.rglob("test_*.py"):
        if any(part in _NON_PACKAGE for part in path.parts):
            continue
        if (path.parent / "__init__.py").is_file():
            continue  # namespaced by its package — collision impossible
        by_name[path.name].append(str(path.relative_to(TESTS_ROOT)))

    collisions = {name: paths for name, paths in by_name.items() if len(paths) > 1}
    assert not collisions, (
        "same-named test modules in non-package directories collide at "
        f"collection: {collisions}"
    )


def _installed_top_level_names() -> set[str]:
    """Top-level importable names contributed by installed distributions."""
    names: set[str] = set()
    for dist in importlib.metadata.distributions():
        top_level = dist.read_text("top_level.txt")
        if top_level:
            names.update(top_level.split())
        for file in dist.files or ():
            parts = file.parts
            first = parts[0]
            if first.endswith((".dist-info", ".egg-info", ".data", ".pth")):
                continue
            if len(parts) > 1:
                names.add(first)
            elif first.endswith(".py"):
                names.add(first[:-3])
    return names


def test_no_test_package_shadows_an_installed_distribution() -> None:
    """A test package must not steal a top-level name from site-packages.

    ``pythonpath`` in ``pyproject.toml`` includes ``tests``, so every
    ``tests/<dir>/__init__.py`` registers ``<dir>`` as a *top-level* module for
    the entire pytest session — ahead of site-packages. Anything importing the
    real distribution then fails, and only inside pytest.

    Twice now: ``tests/coverage/`` shadowed ``coverage``, which killed
    ``pytest-cov`` outright and meant the documented release gate
    (``uv run pytest --cov=novafabric``) could not run at all; then
    ``tests/packaging/`` shadowed ``packaging``, making ``packaging.version``
    — and therefore ``import presidio_analyzer`` — unimportable under pytest,
    surviving only because the affected tests mocked the module.

    Both were renamed (``coverage_reports``, ``packaging_metadata``). The rule
    is asserted rather than remembered because the breakage is silent until
    something unrelated happens to import the shadowed name.
    """
    installed = _installed_top_level_names()
    # Only *direct* children of tests/ become top-level names; deeper packages
    # are namespaced by their parent. Checked independently of _test_dirs() so a
    # package holding only sub-packages is still caught — it shadows all the same.
    shadows = sorted(
        d.name
        for d in TESTS_ROOT.iterdir()
        if d.is_dir()
        and d.name not in _NON_PACKAGE
        and (d / "__init__.py").is_file()
        and d.name in installed
        and d.name not in _GRANDFATHERED_SHADOWS
    )
    assert not shadows, (
        "these tests/ packages shadow an installed top-level distribution name "
        "and will break imports of the real package for the whole pytest "
        f"session: {shadows}. Rename them (e.g. tests/foo -> tests/foo_<topic>)."
    )

