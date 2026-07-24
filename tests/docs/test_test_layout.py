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

from collections import defaultdict
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parents[1]

#: Directories that hold data, not importable tests.
_NON_PACKAGE = ("fixtures", "__pycache__", ".pytest_cache")


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
