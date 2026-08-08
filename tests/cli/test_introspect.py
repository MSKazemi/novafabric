"""Guards for the CLI introspection contract.

These exist because the contract has already broken once, silently. Typer 0.27
vendored its own Click, ``TyperGroup`` stopped inheriting from the installed
``click.Group``, and five copies of an ``isinstance``-based walk each concluded
the CLI had exactly one command. That surfaced as ~289 "command no longer
exists" assertions in three unrelated modules, was read as a broken CLI, and
led to Typer being pinned below 0.26 — even though ``nova --help`` listed the
same 123 commands on both versions.

The tests below are written so the *next* upstream change fails here first,
with one message that names the cause.
"""

from __future__ import annotations

import pytest

from novafabric.cli.introspect import (
    CliIntrospectionError,
    command_paths,
    root_command,
    subcommands,
    top_level_command_names,
    walk_commands,
)

# A CLI this size losing most of its surface is always a bug, never a refactor.
# Deliberately far below the real count (123 top-level) so ordinary additions and
# removals never touch it, while a collapse to one command fails immediately.
MINIMUM_TOP_LEVEL_COMMANDS = 80
MINIMUM_LEAF_COMMANDS = 200

# Stable commands spanning a plain leaf, a group leaf, and a nested group leaf.
# If introspection silently stops descending, the nested one disappears first.
REPRESENTATIVE_PATHS = {
    "nova capture",
    "nova validate",
    "nova diff",
    "nova seal verify",
    "nova lineage provenance",
}


def test_root_is_a_populated_group() -> None:
    """The failure that started all of this: a root reporting no sub-commands."""
    children = subcommands(root_command())
    assert children is not None, "CLI root is not a command group"
    assert len(children) >= MINIMUM_TOP_LEVEL_COMMANDS


def test_subcommands_duck_types_rather_than_checking_a_class() -> None:
    """Any object exposing a ``commands`` mapping is a group.

    This is the whole fix. It must not regress into an ``isinstance`` check
    against Click, because Typer's vendored Click makes that predicate false for
    a group that works perfectly well.
    """

    class VendoredLookalike:
        """Shaped like a group, related to no Click class at all."""

        commands = {"child": object()}

    assert subcommands(VendoredLookalike()) == {"child": VendoredLookalike.commands["child"]}
    assert subcommands(object()) is None
    # A group that happens to be empty is still a group, not a leaf.
    assert subcommands(type("Empty", (), {"commands": {}})()) == {}


def test_command_paths_covers_the_real_surface() -> None:
    paths = command_paths()
    assert len(paths) >= MINIMUM_LEAF_COMMANDS
    missing = REPRESENTATIVE_PATHS - paths
    assert not missing, f"introspection lost known commands: {sorted(missing)}"


def test_every_path_is_prefixed_and_non_empty() -> None:
    for path in command_paths():
        assert path.startswith("nova "), path
        assert "  " not in path, path


def test_walk_yields_leaves_only() -> None:
    """Groups are traversed, never emitted. `nova seal` is a group, not runnable."""
    paths = command_paths()
    assert "nova seal" not in paths
    assert "nova seal verify" in paths


def test_hidden_commands_are_excluded_by_default() -> None:
    visible = command_paths()
    everything = command_paths(include_hidden=True)
    assert visible <= everything


def test_top_level_names_do_not_descend() -> None:
    names = top_level_command_names()
    assert "seal" in names
    assert not any(" " in name for name in names)


def test_root_command_rejects_a_non_group_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken tree must raise, not return an empty result.

    Returning ``{"nova"}`` is what turned one upstream change into 289 misleading
    assertions, so the empty case is an error by construction.
    """
    import novafabric.cli.introspect as introspect

    monkeypatch.setattr(introspect.typer.main, "get_command", lambda app: object())
    with pytest.raises(CliIntrospectionError, match="commands"):
        introspect.root_command()


def test_root_command_rejects_an_empty_group(monkeypatch: pytest.MonkeyPatch) -> None:
    import novafabric.cli.introspect as introspect

    empty = type("EmptyGroup", (), {"commands": {}})()
    monkeypatch.setattr(introspect.typer.main, "get_command", lambda app: empty)
    with pytest.raises(CliIntrospectionError, match="zero sub-commands"):
        introspect.root_command()


def test_walk_commands_returns_command_objects() -> None:
    """Callers such as the dashboard registry generator need the command, not just its path."""
    for path, command in walk_commands():
        if path == "nova capture":
            assert hasattr(command, "params")
            return
    pytest.fail("nova capture not found")
