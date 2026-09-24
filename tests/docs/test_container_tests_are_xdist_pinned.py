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

"""Every container test is pinned to an xdist group, so one fixture means one container.

Container fixtures are module- or session-scoped, but under pytest-xdist each
worker runs its own session. A module whose tests scatter across N workers
therefore starts N copies of the same container. `tests/metadata_store` fixed
that for its own Postgres years ago; the reasoning was never extended, and on
2026-09-09 measurement showed the cost: `-n 4` over the container tier started
THREE simultaneous janusgraph JVMs at 3.4-4.4 GiB each -- 12.1 GiB of a 12.8 GiB
docker peak, with up to 9 containers alive at once. CI's `unit` job runs this
tier in full on a 16 GB hosted runner, where that does not fit: six runs were
killed by a runner shutdown signal at exactly `[ 38%]` with zero FAILED tests,
which is where the lineage container modules begin.

`tests/conftest.py` now assigns the group from the same fixture closure it uses
for the `container` marker. This guards that logic in both directions, because a
guard that can only fail one way is half a guard:

* a test needing a container fixture gets a group, and tests needing the *same*
  fixture get the *same* group (that is what collapses N containers into one);
* an explicit group already on the item wins, so `tests/metadata_store` and
  `tests/jobs` keep theirs;
* a test needing no container fixture is left ungrouped -- otherwise the whole
  suite would serialise onto one worker and the tier would take hours.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# Loaded by path, NOT as `tests.conftest`: `pythonpath = ["src", ..., "tests"]` puts
# tests/ on sys.path, so its modules are top-level and there is no `tests` package.
# Adding `tests/__init__.py` to create one would shadow an installed distribution
# (see CLAUDE.md -- that mistake silently disabled the coverage gate for six weeks),
# and a bare `import conftest` is ambiguous across this repo's many conftest files.
_CONFTEST_PATH = Path(__file__).resolve().parents[1] / "conftest.py"
_spec = importlib.util.spec_from_file_location("_root_conftest_under_test", _CONFTEST_PATH)
assert _spec is not None and _spec.loader is not None
_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_conftest)

CONTAINER_FIXTURES = _conftest.CONTAINER_FIXTURES
pytest_collection_modifyitems = _conftest.pytest_collection_modifyitems


class _Item:
    """Minimal stand-in for `pytest.Item` covering the marker API the hook uses."""

    def __init__(self, fixturenames: tuple[str, ...], group: str | None = None) -> None:
        self.fixturenames = fixturenames
        self.path = "tests/synthetic/test_x.py"
        self._markers: list[pytest.MarkDecorator] = []
        if group is not None:
            self._markers.append(pytest.mark.xdist_group(group))

    def add_marker(self, marker: pytest.MarkDecorator) -> None:
        # `Item.add_marker` prepends by default, so the most recently added
        # marker is the "closest" one. Mirror that, or precedence is untested.
        self._markers.insert(0, marker)

    def get_closest_marker(self, name: str):
        for m in self._markers:
            if m.name == name:
                return m
        return None

    def group(self) -> str | None:
        m = self.get_closest_marker("xdist_group")
        return m.args[0] if m is not None and m.args else None

    def marks(self) -> set[str]:
        return {m.name for m in self._markers}


def _run(items: list[_Item]) -> list[_Item]:
    pytest_collection_modifyitems(config=None, items=items)  # type: ignore[arg-type]
    return items


def test_container_fixtures_is_not_empty() -> None:
    """Non-vacuity: every assertion below is trivially true against an empty set."""
    assert CONTAINER_FIXTURES, "CONTAINER_FIXTURES is empty; this whole guard is vacuous"


@pytest.mark.parametrize("fixture", sorted(CONTAINER_FIXTURES))
def test_every_container_fixture_gets_a_group(fixture: str) -> None:
    (item,) = _run([_Item((fixture, "tmp_path"))])
    assert "container" in item.marks()
    assert item.group() is not None, (
        f"{fixture!r} is a container fixture but its tests are not pinned to an "
        "xdist group -- every worker that receives one will start its own container"
    )


@pytest.mark.parametrize("fixture", sorted(CONTAINER_FIXTURES))
def test_same_fixture_means_same_group(fixture: str) -> None:
    """The property that actually collapses N containers into one."""
    a, b = _run([_Item((fixture, "tmp_path")), _Item((fixture, "monkeypatch"))])
    assert a.group() == b.group() is not None


def test_distinct_fixtures_get_distinct_groups() -> None:
    """Two different containers must not be forced onto one worker unnecessarily."""
    fixtures = sorted(CONTAINER_FIXTURES)
    groups = {f: _run([_Item((f,))])[0].group() for f in fixtures}
    assert len(set(groups.values())) == len(fixtures), groups


def test_an_explicit_group_is_not_overwritten() -> None:
    """tests/metadata_store and tests/jobs pin their own; theirs is more specific."""
    (item,) = _run([_Item((sorted(CONTAINER_FIXTURES)[0],), group="metadata-store-postgres")])
    assert item.group() == "metadata-store-postgres"


def test_non_container_tests_are_left_unpinned() -> None:
    """Pinning everything would serialise the suite onto a single worker."""
    (item,) = _run([_Item(("tmp_path", "monkeypatch"))])
    assert item.group() is None
    assert "container" not in item.marks()


# ---------------------------------------------------------------------------
# The ordering defect that made the first version of this fix inert.
# ---------------------------------------------------------------------------
#
# Everything above passes whether or not the mark ever reaches pytest-xdist,
# because it calls the hook directly. That is exactly the hole the first attempt
# fell into: the grouping logic was correct, the hook ran, the marks were added,
# and `--dist=loadgroup` ignored all of it -- xdist reads `xdist_group` in its
# own `pytest_collection_modifyitems` and encodes it into the nodeid, so a
# conftest implementation that runs afterwards is invisible. Measured: 4
# containers for a single 10-test module. So assert the ordering too.

_HOOKS = (
    "tests/conftest.py",
    "tests/metadata_store/conftest.py",
)


@pytest.mark.parametrize("relpath", _HOOKS)
def test_collection_hooks_run_before_xdist(relpath: str) -> None:
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / relpath).read_text()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "pytest_collection_modifyitems":
            decorated = any(
                isinstance(d, ast.Call)
                and any(
                    kw.arg == "tryfirst" and getattr(kw.value, "value", False) is True
                    for kw in d.keywords
                )
                for d in node.decorator_list
            )
            assert decorated, (
                f"{relpath}: pytest_collection_modifyitems must be declared "
                "@pytest.hookimpl(tryfirst=True). Without it the hook runs AFTER "
                "pytest-xdist has already read the xdist_group marks, so any mark "
                "added here is silently ignored and --dist=loadgroup starts one "
                "container per worker instead of one per fixture."
            )
            return
    pytest.fail(f"{relpath}: no pytest_collection_modifyitems found — guard is vacuous")
