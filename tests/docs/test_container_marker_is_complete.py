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

"""Every test that needs a Docker daemon is in the `container` tier.

`make test-fast` selects by marker (`-m "not container"`) rather than by
directory. That is strictly better than the `--ignore=tests/metadata_store` it
replaced — which discarded 73 Docker-free tests along with the 41 real ones —
but it moves the failure mode: a *new* container fixture that nobody adds to
`CONTAINER_FIXTURES` would run under `make test-fast` and hang or fail on a
machine with no daemon, instead of being deselected.

So this asserts the tier is complete, by finding daemon-dependent modules from
the code itself rather than from a hand-maintained list. Two directions are
checked, because a guard that can only fail one way is half a guard:

* **completeness** — every module that constructs a testcontainers container, or
  shells out to the `docker` CLI, is reachable by the marker.
* **non-vacuity** — the discovery actually found modules, and every name in
  `CONTAINER_FIXTURES` is a fixture that really exists. A typo there would
  silently un-mark a whole tier while every assertion below still passed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_TESTS = _ROOT / "tests"


def _container_fixtures() -> frozenset[str]:
    """Read `CONTAINER_FIXTURES` out of the root conftest.

    Imported by parsing rather than by `from tests.conftest import ...` so this
    guard keeps working regardless of how the conftest is loaded, and so a
    failure here points at the literal a reader can see.
    """
    tree = ast.parse((_TESTS / "conftest.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if "CONTAINER_FIXTURES" not in targets:
            continue
        value = node.value
        # unwrap the `frozenset({...})` call — `literal_eval` handles the set,
        # not the constructor around it
        if isinstance(value, ast.Call) and value.args:
            value = value.args[0]
        return frozenset(ast.literal_eval(value))
    pytest.fail("tests/conftest.py no longer defines CONTAINER_FIXTURES")


def _test_modules() -> list[Path]:
    return sorted(p for p in _TESTS.rglob("*.py") if "__pycache__" not in p.parts)


def _starts_a_container(tree: ast.AST) -> bool:
    """True if the module *constructs* a container or shells the docker CLI.

    Deliberately structural: half the suite mentions "testcontainers" or
    "docker" in a docstring that points at the tier from the unit-tier twin, and
    a string search would mark all of those.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name.endswith("Container") and name != "Container":
            return True
        # subprocess.run(["docker", ...]) / check_call(["docker", ...])
        for arg in node.args:
            if isinstance(arg, ast.List) and arg.elts:
                first = arg.elts[0]
                if isinstance(first, ast.Constant) and first.value == "docker":
                    return True
    return False


def _is_reachable_by_the_marker(source: str, fixtures: frozenset[str]) -> bool:
    """True if the module is either explicitly marked or fixture-marked.

    `tests/conftest.py` marks any test whose fixture closure contains one of
    `fixtures`, so naming one anywhere in the module is enough — the closure
    covers derived fixtures (`pg_store` -> `postgres_dsn`) on its own.
    """
    if "pytest.mark.container" in source:
        return True
    return any(fixture in source for fixture in fixtures)


def test_every_daemon_dependent_module_is_in_the_container_tier() -> None:
    fixtures = _container_fixtures()
    escapees: list[str] = []
    for path in _test_modules():
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - a syntax error is another test's job
            continue
        if not _starts_a_container(tree):
            continue
        if not _is_reachable_by_the_marker(source, fixtures):
            escapees.append(str(path.relative_to(_ROOT)))

    assert not escapees, (
        "these modules start a container but nothing puts them in the "
        f"`container` tier, so `make test-fast` would run them without a "
        f"daemon: {escapees}\n"
        "Fix by requesting one of tests/conftest.py's CONTAINER_FIXTURES, "
        "adding the new fixture to that set, or marking the tests "
        "`@pytest.mark.container`."
    )


def test_the_discovery_is_not_vacuous() -> None:
    """The scan above must actually be finding the tier it claims to guard."""
    found = [
        path
        for path in _test_modules()
        if _starts_a_container(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert len(found) >= 5, (
        "the daemon-dependent-module scan found almost nothing, so the "
        f"completeness assertion above proves nothing: {found}"
    )


def test_every_declared_container_fixture_exists() -> None:
    """A typo in `CONTAINER_FIXTURES` would silently un-mark a whole tier."""
    defined: set[str] = set()
    for path in _test_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if any("fixture" in ast.unparse(d) for d in node.decorator_list):
                defined.add(node.name)

    missing = sorted(_container_fixtures() - defined)
    assert not missing, (
        f"CONTAINER_FIXTURES names fixtures that do not exist: {missing}. "
        "Every test that was relying on one is now silently unmarked."
    )
