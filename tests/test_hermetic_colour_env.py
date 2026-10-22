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

"""The test session renders CLI output the same way on every machine.

Typer decides at *import time* whether to force a colour terminal, from
``GITHUB_ACTIONS``, ``FORCE_COLOR`` or ``PY_COLORS``. GitHub sets the first, so
Rich wrote ANSI escapes *inside* option names in CI and nowhere else — and a plain
``"--identity" in result.output`` failed only there. That class reached ``main``
at least twice (issue #21, then the v0.106.0 promote tests); ``_help_assert`` is
the per-test workaround, and the 59th test simply did not use it.

``tests/conftest.py`` now pins the environment before Typer is imported. This
asserts the pin is in effect *in this process*, which is the property that
matters: in CI, ``GITHUB_ACTIONS`` is set and this still has to hold.
"""

from __future__ import annotations

import os

import typer.rich_utils


def test_typer_never_forces_a_colour_terminal_under_test() -> None:
    assert typer.rich_utils.FORCE_TERMINAL is False, (
        "Typer is forcing (or auto-detecting) a terminal in the test session, so "
        "help output differs between CI and a laptop. tests/conftest.py must set "
        "_TYPER_FORCE_DISABLE_TERMINAL before anything imports typer."
    )


def test_inherited_colour_forcing_is_dropped() -> None:
    for name in ("FORCE_COLOR", "PY_COLORS", "COLORTERM"):
        assert name not in os.environ, (
            f"{name} leaked into the test session from the caller's shell; tests "
            "that need colour must set it themselves (monkeypatch / subprocess env)"
        )
