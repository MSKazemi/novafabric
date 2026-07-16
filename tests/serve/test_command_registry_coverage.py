"""Guard: the dashboard CommandsTab must mirror the complete `nova` CLI.

The dashboard ships a generated command registry
(`web/src/components/dashboard/commands/generatedCommands.ts`) produced by
`web/scripts/gen-command-registry.py` from the live Typer app. This test fails
if the checked-in file drifts from the CLI — i.e. a command was added or removed
without regenerating the registry — so "every CLI capability is in the
dashboard" stays true over time.

Regenerate with:
    uv run python web/scripts/gen-command-registry.py
"""
from __future__ import annotations

import re
from pathlib import Path

import click
import pytest
import typer.main

from novafabric.cli.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATED_TS = (
    REPO_ROOT / "web/src/components/dashboard/commands/generatedCommands.ts"
)


def _cli_command_paths() -> set[str]:
    """Every runnable `nova …` command path (leaf commands, excluding hidden)."""
    root = typer.main.get_command(app)
    paths: set[str] = set()

    def walk(cmd: click.Command, path: str) -> None:
        if isinstance(cmd, click.Group):
            for name, sub in cmd.commands.items():
                walk(sub, f"{path} {name}")
            return
        if getattr(cmd, "hidden", False):
            return
        paths.add(path)

    walk(root, "nova")
    return paths


def _registry_command_names() -> set[str]:
    text = GENERATED_TS.read_text(encoding="utf-8")
    # Each generated def carries a `"name": "nova …"` line.
    return set(re.findall(r'"name":\s*"(nova [^"]+)"', text))


def test_generated_registry_file_exists() -> None:
    assert GENERATED_TS.is_file(), (
        f"{GENERATED_TS} is missing — run "
        "`uv run python web/scripts/gen-command-registry.py`."
    )


def test_every_cli_command_is_in_the_dashboard() -> None:
    cli = _cli_command_paths()
    registry = _registry_command_names()

    missing = sorted(cli - registry)
    assert not missing, (
        f"{len(missing)} CLI command(s) are not surfaced in the dashboard "
        f"CommandsTab: {missing}. Regenerate with "
        "`uv run python web/scripts/gen-command-registry.py`."
    )


def test_no_stale_commands_in_the_dashboard() -> None:
    cli = _cli_command_paths()
    registry = _registry_command_names()

    stale = sorted(registry - cli)
    assert not stale, (
        f"{len(stale)} command(s) in the dashboard registry no longer exist in "
        f"the CLI: {stale}. Regenerate with "
        "`uv run python web/scripts/gen-command-registry.py`."
    )


@pytest.mark.parametrize("_run", [0])
def test_coverage_is_complete(_run: int) -> None:
    """Sanity: the registry covers the full surface (>= 200 commands today)."""
    assert _cli_command_paths() == _registry_command_names()
