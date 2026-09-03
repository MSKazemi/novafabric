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

"""The dev-loop worker throttle stays wired and sane.

Every test tier runs `-n auto`, which pytest-xdist resolves to ALL cores unless
`PYTEST_XDIST_AUTO_NUM_WORKERS` says otherwise. On 2026-09-04 this machine was
measured at load 49-65 on 20 cores with 11 GiB in swap because several sessions
each ran full-width suites at once. The fix is `scripts/test-workers.sh` exported
through the Makefile — and the fix only holds while three things stay true:

1. the script exists, is executable, and prints a bounded positive integer;
2. the Makefile export is present, so every `make test-*` inherits it;
3. no pytest recipe hardcodes `-n <count>` — that would bypass the env var and
   silently reintroduce a fixed-width run (and break CI parity for `test-par`).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "test-workers.sh"
MAKEFILE = (REPO / "Makefile").read_text()


def _run(
    env_extra: dict[str, str] | None = None, args: list[str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "NOVA_TEST_WORKERS"}
    env.update(env_extra or {})
    return subprocess.run(
        [str(SCRIPT), *(args or [])],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=REPO,
    )


def test_script_is_executable() -> None:
    assert SCRIPT.is_file(), "scripts/test-workers.sh is missing"
    assert os.access(SCRIPT, os.X_OK), "scripts/test-workers.sh is not executable"


def test_script_prints_a_bounded_positive_integer() -> None:
    proc = _run()
    assert proc.returncode == 0, proc.stderr
    n = int(proc.stdout.strip())  # non-integer output raises ValueError -> fail
    nproc = os.cpu_count() or 1
    assert 1 <= n <= nproc, f"worker count {n} outside [1, {nproc}]"


def test_explicit_override_wins() -> None:
    proc = _run({"NOVA_TEST_WORKERS": "3"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "3"


def test_non_numeric_override_is_refused() -> None:
    proc = _run({"NOVA_TEST_WORKERS": "many"})
    assert proc.returncode != 0
    assert "not a number" in proc.stderr


def test_makefile_exports_the_throttle() -> None:
    assert re.search(
        r"^export PYTEST_XDIST_AUTO_NUM_WORKERS \?= \$\(shell \./scripts/test-workers\.sh\)$",
        MAKEFILE,
        re.MULTILINE,
    ), "Makefile no longer exports PYTEST_XDIST_AUTO_NUM_WORKERS from scripts/test-workers.sh"


def test_gate_mode_grants_at_least_as_many_workers() -> None:
    """--gate floors at nproc/2: a serial push gate (measured 34 min) is worse
    than a briefly oversubscribed one. Memory-capped, so >= is the invariant."""
    normal = _run()
    gate = _run(args=["--gate"])
    assert gate.returncode == 0, gate.stderr
    n_gate = int(gate.stdout.strip())
    nproc = os.cpu_count() or 1
    assert int(normal.stdout.strip()) <= n_gate <= nproc


def test_gate_script_exists_and_prepush_hook_delegates_to_it() -> None:
    """The hand-run gate and the pre-push gate must be the same code path —
    two copies of digest/stamp logic is how they drift apart."""
    gate = REPO / "scripts" / "test-gate.sh"
    assert gate.is_file() and os.access(gate, os.X_OK)
    hook = (REPO / "scripts" / "hooks" / "pre-push-test-gate.sh").read_text()
    assert "./scripts/test-gate.sh" in hook, "pre-push no longer delegates to scripts/test-gate.sh"
    assert "sha256sum" not in hook, "pre-push grew its own digest logic — it must live only in test-gate.sh"
    assert re.search(r"^test-gate:\n\t\./scripts/test-gate\.sh$", MAKEFILE, re.MULTILINE), (
        "Makefile lost the test-gate target"
    )


def test_no_pytest_recipe_hardcodes_a_worker_count() -> None:
    hardcoded = [
        line.strip()
        for line in MAKEFILE.splitlines()
        if "pytest" in line and re.search(r"-n\s*=?\s*\d+", line)
    ]
    assert not hardcoded, f"pytest recipes hardcode -n <count>, bypassing the throttle: {hardcoded}"
