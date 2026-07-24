"""Guard: every `nova` CLI command carries an honest dashboard-parity class.

The dashboard ships a parity registry
(`web/src/components/dashboard/commands/commandParity.json`) classifying every
non-hidden CLI leaf command as one of:

- ``real-panel``  — a dashboard tab has a real panel backed by a server
  endpoint (the ``api`` value must appear in both ``web/src/lib/api.ts`` and
  ``src/novafabric/serve/``);
- ``builder-only`` — only the copy-only Commands-tab builder (ADR-0027
  Layer C) covers it — the honest default;
- ``cli-only``    — intentionally has no panel (process lifecycle, local
  filesystem, CI-oriented …) with a one-line ``reason``.

This test fails when a command is added or removed without updating the
registry, when a status value is invalid, or when a ``real-panel`` claim is no
longer backed by evidence — so the classification can only be upgraded with
proof (a ratchet, not a wish).

Seed / inspect missing entries with:
    uv run python web/scripts/gen-parity-skeleton.py
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

import click
import typer.main

from novafabric.cli.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
PARITY_JSON = (
    REPO_ROOT / "web/src/components/dashboard/commands/commandParity.json"
)
API_TS = REPO_ROOT / "web/src/lib/api.ts"
SERVE_DIR = REPO_ROOT / "src/novafabric/serve"
SIDEBAR_TSX = REPO_ROOT / "web/src/components/dashboard/Sidebar.tsx"

VALID_STATUSES = {"real-panel", "builder-only", "cli-only"}


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


@lru_cache(maxsize=1)
def _parity() -> dict[str, dict]:
    assert PARITY_JSON.is_file(), (
        f"{PARITY_JSON} is missing — seed it with "
        "`uv run python web/scripts/gen-parity-skeleton.py`."
    )
    data = json.loads(PARITY_JSON.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "commandParity.json must be a JSON object"
    return data


@lru_cache(maxsize=1)
def _api_ts_text() -> str:
    return API_TS.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _serve_text() -> str:
    return "\n".join(
        f.read_text(encoding="utf-8") for f in sorted(SERVE_DIR.rglob("*.py"))
    )


@lru_cache(maxsize=1)
def _dashboard_tab_ids() -> set[str]:
    """The `Tab` union in Sidebar.tsx is the source of truth for tab ids."""
    text = SIDEBAR_TSX.read_text(encoding="utf-8")
    union = text.split("export type Tab =", 1)[1].split(";", 1)[0]
    return set(re.findall(r"'([a-z0-9-]+)'", union))


def test_every_cli_command_is_classified() -> None:
    cli = _cli_command_paths()
    classified = set(_parity())

    missing = sorted(cli - classified)
    assert not missing, (
        f"{len(missing)} CLI command(s) have no parity classification: "
        f"{missing}. Add entries to {PARITY_JSON.name} "
        "(`uv run python web/scripts/gen-parity-skeleton.py` lists them; "
        '{"status": "builder-only"} is the honest default).'
    )


def test_no_stale_classifications() -> None:
    cli = _cli_command_paths()
    classified = set(_parity())

    stale = sorted(classified - cli)
    assert not stale, (
        f"{len(stale)} entr(y/ies) in {PARITY_JSON.name} no longer exist in "
        f"the CLI: {stale}. Remove them."
    )


def test_statuses_are_valid() -> None:
    bad = {
        cmd: entry.get("status")
        for cmd, entry in _parity().items()
        if entry.get("status") not in VALID_STATUSES
    }
    assert not bad, (
        f"Invalid status values (must be one of {sorted(VALID_STATUSES)}): {bad}"
    )


def test_cli_only_entries_have_a_reason() -> None:
    bad = sorted(
        cmd
        for cmd, entry in _parity().items()
        if entry.get("status") == "cli-only"
        and not str(entry.get("reason", "")).strip()
    )
    assert not bad, (
        f"cli-only entries must carry a non-empty one-line 'reason': {bad}"
    )


def test_real_panel_entries_have_tab_and_api() -> None:
    bad = sorted(
        cmd
        for cmd, entry in _parity().items()
        if entry.get("status") == "real-panel"
        and (
            not str(entry.get("tab", "")).strip()
            or not str(entry.get("api", "")).strip()
        )
    )
    assert not bad, f"real-panel entries must carry 'tab' and 'api': {bad}"


def test_real_panel_tabs_exist_in_the_sidebar() -> None:
    tabs = _dashboard_tab_ids()
    bad = {
        cmd: entry["tab"]
        for cmd, entry in _parity().items()
        if entry.get("status") == "real-panel" and entry.get("tab") not in tabs
    }
    assert not bad, (
        f"real-panel 'tab' must be a dashboard tab id from Sidebar.tsx "
        f"({sorted(tabs)}): {bad}"
    )


def test_real_panel_api_is_backed_by_client_and_server() -> None:
    """The evidence ratchet: a real-panel claim needs the endpoint on BOTH sides."""
    api_ts = _api_ts_text()
    serve = _serve_text()

    not_in_client = sorted(
        cmd
        for cmd, entry in _parity().items()
        if entry.get("status") == "real-panel" and entry["api"] not in api_ts
    )
    assert not_in_client == [], (
        "real-panel 'api' value not found in web/src/lib/api.ts for: "
        f"{not_in_client} — downgrade to builder-only or fix the endpoint."
    )

    not_in_server = sorted(
        cmd
        for cmd, entry in _parity().items()
        if entry.get("status") == "real-panel" and entry["api"] not in serve
    )
    assert not_in_server == [], (
        "real-panel 'api' value not found under src/novafabric/serve/ for: "
        f"{not_in_server} — downgrade to builder-only or fix the endpoint."
    )
