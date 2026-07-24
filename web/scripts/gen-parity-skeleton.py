#!/usr/bin/env python3
"""Print `nova` CLI commands missing from commandParity.json (and stale ones).

Seeds new entries for the CLI-parity classification registry
(`web/src/components/dashboard/commands/commandParity.json`, guarded by
`tests/serve/test_command_parity_classification.py`). For each missing command
it prints a ready-to-paste `builder-only` entry — the honest default; upgrade
to `real-panel` only with endpoint evidence on both client and server.

Usage:
    uv run python web/scripts/gen-parity-skeleton.py
"""
from __future__ import annotations

import json
from pathlib import Path

import click
import typer.main

from novafabric.cli.main import app

PARITY_JSON = (
    Path(__file__).resolve().parent.parent
    / "src/components/dashboard/commands/commandParity.json"
)


def cli_command_paths() -> set[str]:
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


def main() -> None:
    cli = cli_command_paths()
    known = (
        set(json.loads(PARITY_JSON.read_text(encoding="utf-8")))
        if PARITY_JSON.is_file()
        else set()
    )
    missing = sorted(cli - known)
    stale = sorted(known - cli)

    for cmd in missing:
        print(f'  {json.dumps(cmd)}: {{"status": "builder-only"}},')
    if stale:
        print(f"# stale (remove from {PARITY_JSON.name}): {stale}")
    print(f"# {len(missing)} missing, {len(stale)} stale, {len(cli)} CLI commands")


if __name__ == "__main__":
    main()
