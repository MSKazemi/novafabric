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

"""An install hint must still name the extra after Rich renders it.

``pip install 'novafabric[serve]'`` printed through a Rich console rendered as
``pip install 'novafabric'``: Rich reads ``[serve]`` as a markup tag and drops it.
The instruction lost the only token that made it do anything, silently, in five
call sites — and it fired precisely when a user was stuck on a missing extra.

The failure is invisible to a normal test, because the source string is correct.
Only rendering it catches this, so that is what these do.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from rich.console import Console

from novafabric.cli._extras import (
    declared_extras,
    extra_requirements,
    install_command,
    missing_requirements,
    rich_install_command,
)

_SRC = Path(__file__).resolve().parents[2] / "src" / "novafabric"

#: A Rich render call whose statement contains an install hint.
_RICH_CALL = re.compile(r"(console\.print|Panel|rich_print)\s*\(")


def _render(markup: str) -> str:
    buf = io.StringIO()
    Console(file=buf, width=200, no_color=True, markup=True).print(markup)
    return buf.getvalue().strip()


def test_rich_install_command_survives_rendering() -> None:
    rendered = _render(rich_install_command("serve"))

    assert "novafabric[serve]" in rendered, (
        f"the extra name was stripped by Rich: {rendered!r}"
    )


def test_the_unescaped_form_is_the_bug_this_guards() -> None:
    """Red-green anchor: without escaping, the extra name disappears.

    If Rich ever stops treating ``[serve]`` as markup this fails, which is the
    signal to simplify rather than a defect.
    """
    rendered = _render(install_command("serve"))

    assert "novafabric[serve]" not in rendered
    assert "pip install 'novafabric'" in rendered


def test_install_command_is_shell_quoted() -> None:
    """Unquoted brackets are glob characters; zsh fails on them outright."""
    assert install_command("serve") == "pip install 'novafabric[serve]'"


@pytest.mark.parametrize("extra", ["serve", "spkg", "server", "query"])
def test_every_extra_renders_intact(extra: str) -> None:
    assert extra in _render(rich_install_command(extra))


def test_no_source_file_prints_an_unescaped_install_hint_through_rich() -> None:
    """The guard proper: catches a new call site written the broken way.

    Scans for an install hint inside a Rich render call that is neither escaped
    nor produced by ``rich_install_command``.
    """
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        if path.name == "_extras.py":
            continue
        lines = path.read_text(encoding="utf-8").split("\n")
        for i, line in enumerate(lines):
            if "novafabric[" not in line:
                continue
            if "rich_install_command" in line or r"novafabric\[" in line:
                continue
            statement = "\n".join(lines[max(0, i - 6) : i + 1])
            if _RICH_CALL.search(statement):
                offenders.append(f"{path.relative_to(_SRC)}:{i + 1}: {line.strip()[:80]}")

    assert not offenders, (
        "these print an install hint through Rich without escaping, so the extra "
        "name will be stripped from the rendered output — use "
        "rich_install_command():\n  " + "\n  ".join(offenders)
    )


def test_declared_extras_come_from_distribution_metadata() -> None:
    """Not hardcoded: the set moves when pyproject moves."""
    extras = declared_extras()

    assert extras, "no extras found — distribution metadata not readable"
    assert "serve" in extras
    assert extras == sorted(extras)


def test_extra_requirements_parses_the_single_quoted_marker() -> None:
    """``extra == 'serve'`` is single-quoted here; matching only \" finds nothing."""
    reqs = extra_requirements("serve")

    assert reqs, "no requirements parsed for the serve extra"
    assert any(r.startswith("fastapi") for r in reqs), reqs


def test_missing_requirements_is_empty_for_an_installed_extra() -> None:
    """The dev environment installs all extras, so serve must report complete."""
    assert missing_requirements("serve") == []


def test_missing_requirements_of_an_unknown_extra_is_empty_not_an_error() -> None:
    assert missing_requirements("no-such-extra") == []
