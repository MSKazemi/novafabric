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

"""CI and the published GitHub Action install Python packages with uv, never pip.

uv.lock is the source of truth for every environment CI builds. A stray
`pip install` or `pipx run` resolves on its own, outside the lock, unpinned
unless someone remembers to pin it. The last two (`pipx run pip-audit`, the
capture action's `python -m pip install`) were exactly that.
"""

from __future__ import annotations

import re
from pathlib import Path

_GITHUB = Path(__file__).resolve().parents[2] / ".github"

#: An invocation of pip or pipx, not a mention of it in prose or in a tool name
#: such as `pip-audit`. `uv pip install` is uv, and stays allowed.
_PIP = re.compile(r"(?<!uv )(?<![\w-])(?:python3?\s+-m\s+pip|pipx?)\s+(?:install|run|download)\b")


def _executable_lines() -> list[tuple[str, int, str]]:
    found = []
    for path in sorted(_GITHUB.rglob("*.y*ml")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.lstrip().startswith("#"):
                found.append((str(path.relative_to(_GITHUB.parent)), number, line))
    return found


def test_no_workflow_or_action_installs_with_pip() -> None:
    offenders = [
        f"{p}:{n}: {line.strip()}" for p, n, line in _executable_lines() if _PIP.search(line)
    ]
    assert not offenders, "use uv (`uv sync`, `uvx`, `uv pip install`), not pip:\n  " + "\n  ".join(
        offenders
    )


def test_the_pattern_catches_what_it_must_and_spares_the_rest() -> None:
    must = [
        "python -m pip install --quiet x",
        "python3 -m pip install x",
        "pip install novafabric",
        "pipx run pip-audit -r r.txt",
        "  run: pip download x",
    ]
    spare = [
        'uv pip install --python "$(command -v python)" x',
        'uvx --from "pip-audit==2.10.1" pip-audit -r r.txt',
        "name: pip-audit (locked deps)",
        "python3 scripts/pip_audit_gate.py --report pip-audit-report.json",
    ]
    assert all(_PIP.search(s) for s in must), [s for s in must if not _PIP.search(s)]
    assert not any(_PIP.search(s) for s in spare), [s for s in spare if _PIP.search(s)]


def test_the_sweep_is_not_vacuous() -> None:
    lines = _executable_lines()
    assert len(lines) > 1000, f"expected the whole .github tree, read {len(lines)} lines"
    assert any("actions/capture/action.yml" in p for p, _, _ in lines)
