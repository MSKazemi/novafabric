"""Guard: docs/cli-reference.md must cover every top-level `nova` command.

The dashboard command registry is drift-guarded
(tests/serve/test_command_registry_coverage.py), but the Markdown CLI
reference used to drift freely — the 2026-07-16 audit found 26 registered
top-level commands with no reference section. This guard keeps the doc
honest: every visible top-level command (group or leaf) must be mentioned
as ``nova <name>`` somewhere in the reference.

Scope note: deliberately top-level only. Requiring every one of the ~264
leaf paths would force boilerplate sections; a top-level mention with the
group's subcommands documented in its section is the bar the existing doc
style sets.
"""

from __future__ import annotations

import re
from pathlib import Path

import click
import typer.main

from novafabric.cli.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_REFERENCE = REPO_ROOT / "docs" / "cli-reference.md"


def _top_level_commands() -> set[str]:
    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)
    return {
        name
        for name, sub in root.commands.items()
        if not getattr(sub, "hidden", False)
    }


def test_cli_reference_exists() -> None:
    assert CLI_REFERENCE.is_file()


def test_every_top_level_command_is_documented() -> None:
    text = CLI_REFERENCE.read_text(encoding="utf-8")
    missing = sorted(
        name
        for name in _top_level_commands()
        if not re.search(rf"nova {re.escape(name)}\b", text)
    )
    assert not missing, (
        f"{len(missing)} top-level command(s) have no section in "
        f"docs/cli-reference.md: {missing}. Document each new command when "
        "it ships (docs honesty rule) — see the file's existing section style."
    )


def test_no_documented_commands_that_no_longer_exist() -> None:
    """Headings claiming a `nova <cmd>` that the CLI no longer registers."""
    text = CLI_REFERENCE.read_text(encoding="utf-8")
    known = _top_level_commands()
    # Only inspect Markdown headings — prose may legitimately mention
    # historic names, and `#` lines inside code fences are shell comments.
    stale: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line.startswith("#"):
            continue
        for match in re.findall(r"`?nova ([a-z][a-z0-9-]*)\b", line):
            if match not in known:
                stale.append(match)
    assert not stale, (
        f"cli-reference.md headings reference command(s) not in the CLI: "
        f"{sorted(set(stale))}"
    )
