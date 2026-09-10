"""Every registered command can render its own ``--help``.

Defect **B5**: `nova export-blob --help` exited 1 with

    MarkupError: closing tag '[/prefix]' at position 60 doesn't match any open tag

because a Typer ``help=`` string contained the literal text ``s3://bucket[/prefix]``
and Rich parsed ``[/prefix]`` as a closing markup tag. The command was shipped,
documented, and its help was unreadable to every user who asked for it.

**Why nothing caught it.** `tests/test_cli.py::test_help_shows_all_commands`
renders only the **top-level** `--help` and checks that the command *names*
appear. The root help renders fine — it never touches a subcommand's help text.
So a command whose own help crashes passes CI.

This test walks the whole command tree and renders every node's help, which is
the assertion that would have failed on day one. A scan at the time of writing
found **1 failure across 398 command paths**, so the guard is nearly free and
its signal is specific.

Related: the same "assert the property over all N sites, once" reasoning as
ADR-0270 / `tests/test_runners_env_forwarding.py`.
"""
from __future__ import annotations

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _walk(command, path: list[str]):
    """Yield ``(path, command)`` for the command and every nested subcommand."""
    yield path, command
    for name, sub in (getattr(command, "commands", None) or {}).items():
        yield from _walk(sub, [*path, name])


def _all_command_paths() -> list[str]:
    return [" ".join(p) for p, _ in _walk(get_command(app), []) if p]


ALL_COMMAND_PATHS = _all_command_paths()


def test_the_command_tree_is_actually_populated() -> None:
    """Guard the guard: if the walk returns nothing, every parametrised case
    below would vacuously pass and this file would assert nothing at all."""
    assert len(ALL_COMMAND_PATHS) > 100, (
        f"expected the full nova command tree, walked only {len(ALL_COMMAND_PATHS)}: "
        f"{ALL_COMMAND_PATHS[:10]}"
    )


@pytest.mark.parametrize("command_path", ALL_COMMAND_PATHS, ids=lambda p: p.replace(" ", "-"))
def test_command_help_renders(command_path: str) -> None:
    """``nova <command> --help`` must exit 0 and print usage.

    Rich raises `MarkupError` on an unbalanced ``[tag]`` in help text, so a
    literal square bracket has to be escaped as ``\\[`` — the convention already
    used in `cli/verify.py` and `cli/_extras.py`.
    """
    result = runner.invoke(app, [*command_path.split(" "), "--help"])
    assert result.exit_code == 0, (
        f"`nova {command_path} --help` exited {result.exit_code}: "
        f"{result.exception!r}\n"
        f"A literal '[' in a help string must be escaped as r'\\[' — Rich parses "
        f"'[...]' as markup."
    )
