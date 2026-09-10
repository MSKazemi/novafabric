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

from novafabric.cli.introspect import top_level_command_names

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_REFERENCE = REPO_ROOT / "docs" / "cli-reference.md"


def _top_level_commands() -> set[str]:
    return top_level_command_names()


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


# ---------------------------------------------------------------------------
# Leaf coverage — the scope note above says "deliberately top-level only",
# on the reasoning that requiring every leaf would force boilerplate sections.
#
# Measured 2026-09-10: that fear did not materialise. All **263** subcommand
# paths are already documented, across cli-reference and the guides. So this
# holds what the docs have actually achieved rather than demanding new prose —
# a mention of `nova <group> <sub>` anywhere in the user-facing set, which is
# the same bar the top-level guard uses.
#
# ⚠ It looks at the same working tree the CLI is imported from. An earlier
# measurement compared HEAD's *docs* against the working tree's *commands* and
# reported five phantom gaps (`nova dashboard …`) — a feature whose module,
# registration and documentation are all uncommitted together. Comparing two
# different states of the tree invents drift that does not exist.
# ---------------------------------------------------------------------------

USER_FACING = (
    REPO_ROOT / "docs" / "cli-reference.md",
    REPO_ROOT / "docs" / "user-guide.md",
    REPO_ROOT / "docs" / "operator-guide.md",
    REPO_ROOT / "docs" / "getting-started.md",
    REPO_ROOT / "docs" / "developer-guide.md",
    REPO_ROOT / "README.md",
)


def _leaf_command_paths() -> list[str]:
    from typer.main import get_command

    from novafabric.cli.main import app

    def walk(command, path):
        yield path, command
        for name, sub in (getattr(command, "commands", None) or {}).items():
            yield from walk(sub, [*path, name])

    return [" ".join(p) for p, _ in walk(get_command(app), []) if len(p) > 1]


def test_the_command_tree_has_subcommands() -> None:
    """Guard the guard: an empty walk would make the check below vacuous."""
    leaves = _leaf_command_paths()
    assert len(leaves) > 100, f"walked only {len(leaves)} subcommands: {leaves[:5]}"


def test_every_subcommand_is_mentioned_somewhere() -> None:
    text = "\n".join(p.read_text(encoding="utf-8") for p in USER_FACING if p.is_file())
    missing = sorted(p for p in _leaf_command_paths() if f"nova {p}" not in text)
    assert not missing, (
        f"{len(missing)} subcommand(s) are registered but appear in no "
        f"user-facing document: {missing}. A user can run these today and find "
        f"nothing written about them."
    )
