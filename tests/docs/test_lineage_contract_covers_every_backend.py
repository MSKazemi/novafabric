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

"""Every lineage backend runs the shared contract.

`tests/lineage/contract.py` only buys anything if every backend actually runs
it. Before it existed each backend was checked by its own hand-written file, and
the files did not agree on what to assert — Kuzu asserted `len(result) >= 1`
where SQLite asserted exact refs, and two real divergences lived in that gap.

Nothing stops the next backend from repeating the pattern except this test, so
it is deliberately structural: it enumerates the concrete `AbstractLineageStore`
subclasses that ship in `src/`, and requires each to appear in a test module
that parametrises over `CONTRACT_CHECKS`.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_BACKENDS = _ROOT / "src" / "novafabric" / "lineage" / "backends"
_LINEAGE_TESTS = _ROOT / "tests" / "lineage"


def _concrete_backends() -> dict[str, Path]:
    """Concrete `AbstractLineageStore` subclasses shipped under `backends/`."""
    found: dict[str, Path] = {}
    for path in sorted(_BACKENDS.glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {ast.unparse(b) for b in node.bases}
            if "AbstractLineageStore" in bases:
                found[node.name] = path
    return found


def _modules_running_the_contract() -> dict[Path, str]:
    """Test modules that parametrise over the shared contract."""
    running: dict[Path, str] = {}
    for path in sorted(_LINEAGE_TESTS.glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        if "CONTRACT_CHECKS" in source and "contract_params" in source:
            running[path] = source
    return running


def test_every_backend_runs_the_shared_contract() -> None:
    backends = _concrete_backends()
    assert backends, "no concrete lineage backends found — the scan is broken"

    running = _modules_running_the_contract()
    all_source = "\n".join(running.values())

    uncovered = sorted(name for name in backends if name not in all_source)
    assert not uncovered, (
        f"these lineage backends ship but never run the shared contract: {uncovered}.\n"
        "Add a contract class to their test module (see "
        "tests/lineage/test_backends_sqlite.py). A backend verified only by its "
        "own hand-written assertions is how Kuzu came to return unordered "
        "replay chains and empty asset refs while its tests passed.\n"
        "If the backend genuinely cannot satisfy a check, declare it in "
        "`contract_params({...})` — that records the gap as a strict xfail "
        "instead of hiding it."
    )


def test_the_reference_backend_declares_no_divergence() -> None:
    """SQLite is the reference: if *it* needs an exemption, the contract is wrong."""
    source = (_LINEAGE_TESTS / "test_backends_sqlite.py").read_text(encoding="utf-8")
    assert "contract.contract_params()" in source, (
        "the SQLite contract run must call contract_params() with no arguments. "
        "The reference implementation defines the contract — exempting it from a "
        "check means the check no longer describes anything."
    )


def test_the_scan_is_not_vacuous() -> None:
    """Both halves must really be finding things."""
    backends = _concrete_backends()
    running = _modules_running_the_contract()
    assert len(backends) >= 4, f"expected the shipped backends, found {sorted(backends)}"
    assert len(running) >= 4, (
        f"expected several modules running the contract, found {sorted(running)}"
    )
