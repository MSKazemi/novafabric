"""Version-tolerant introspection of the ``nova`` command tree.

Documentation gates, the dashboard command registry, and the CLI/panel parity
classification all need the same thing: every runnable ``nova …`` path. Until
this module existed each of them carried its own copy of the walk, and every
copy identified a command group with ``isinstance(cmd, click.Group)``.

That predicate is not stable. Typer 0.27 vendors its own Click as
``typer._click``, so ``TyperGroup`` stopped inheriting from the *installed*
``click.Group`` while remaining a perfectly functional group:

    typer 0.25.1   TyperGroup -> click.core.Group -> click.core.Command
    typer 0.27.1   TyperGroup -> typer._click.core.Command -> abc.ABC

Every walker then reported the CLI as a single command named ``nova``, which
surfaced as ~289 "this command no longer exists" assertions across three test
modules. The CLI itself was never affected — ``nova --help`` lists the same 123
commands on both versions and a capture round-trip succeeds on either — but the
failure was read as a broken CLI and Typer was pinned below 0.26 to avoid it.
A misdiagnosis froze a runtime dependency for the whole project.

Two decisions follow from that, and both matter more than the code:

1. **Duck-type on ``.commands``, never on a vendored class.** Any object that
   maps names to sub-commands is a group. This holds across Click 7/8/9 and any
   future Typer vendoring, because it depends on the shape callers actually use
   rather than on an inheritance edge that is an implementation detail.

2. **Fail loudly, never silently return an empty tree.** An introspection that
   quietly finds nothing is indistinguishable from a CLI that legitimately lost
   every command, and it produces a blizzard of confusing downstream assertions
   instead of one clear error. :func:`root_command` refuses to return a root
   that is not a populated group.

Public API: :func:`command_paths`, :func:`top_level_command_names`,
:func:`walk_commands`, :func:`root_command`, :func:`subcommands`.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import typer
import typer.main

__all__ = [
    "CliIntrospectionError",
    "command_paths",
    "root_command",
    "subcommands",
    "top_level_command_names",
    "walk_commands",
]

#: Root name every command path is prefixed with, matching the console script.
ROOT_NAME = "nova"


class CliIntrospectionError(RuntimeError):
    """The CLI command tree could not be read in the expected shape.

    Raised instead of returning an empty or degenerate tree, so that an upstream
    change to Typer or Click fails once with an actionable message rather than
    as many confusing assertions in unrelated test modules.
    """


def subcommands(command: object) -> Mapping[str, Any] | None:
    """Return ``command``'s sub-commands, or ``None`` if it is a leaf.

    Identifies a group by the presence of a ``commands`` mapping rather than by
    class, which is what keeps this working across Click versions and Typer's
    vendored Click. A group with no sub-commands is still a group.
    """
    candidate = getattr(command, "commands", None)
    if isinstance(candidate, Mapping):
        return candidate
    return None


def root_command(app: typer.Typer | None = None) -> Any:
    """Return the Click-compatible root command for ``app``.

    Args:
        app: The Typer application. Defaults to the ``nova`` CLI.

    Raises:
        CliIntrospectionError: If the root is not a group, or is an empty one.
            Both mean the introspection contract broke upstream; neither should
            be reported as "the CLI has no commands".
    """
    if app is None:
        from novafabric.cli.main import app as nova_app

        app = nova_app

    root = typer.main.get_command(app)
    children = subcommands(root)

    if children is None:
        raise CliIntrospectionError(
            f"Expected the CLI root to expose a 'commands' mapping, but "
            f"{type(root)!r} does not. Typer or Click changed how the command "
            f"tree is represented; update novafabric.cli.introspect.subcommands "
            f"to match, and do not conclude that the CLI lost its commands "
            f"before checking `nova --help`."
        )
    if not children:
        raise CliIntrospectionError(
            "The CLI root reports zero sub-commands. Either command "
            "registration genuinely failed at import time, or Typer changed the "
            "tree shape. Check `nova --help` before assuming the former."
        )
    return root


def walk_commands(
    app: typer.Typer | None = None,
    *,
    root_name: str = ROOT_NAME,
    include_hidden: bool = False,
) -> Iterator[tuple[str, Any]]:
    """Yield ``(path, command)`` for every leaf command, depth-first by name.

    Groups are traversed but never yielded — only runnable commands are. Paths
    are space-joined and prefixed with ``root_name``, e.g. ``"nova seal verify"``.

    Args:
        app: The Typer application. Defaults to the ``nova`` CLI.
        root_name: Prefix for every path.
        include_hidden: Whether to include commands marked ``hidden``.
    """
    root = root_command(app)

    def visit(command: Any, path: str) -> Iterator[tuple[str, Any]]:
        children = subcommands(command)
        if children is not None:
            for name, child in sorted(children.items()):
                yield from visit(child, f"{path} {name}")
            return
        if not include_hidden and getattr(command, "hidden", False):
            return
        yield path, command

    yield from visit(root, root_name)


def command_paths(
    app: typer.Typer | None = None,
    *,
    root_name: str = ROOT_NAME,
    include_hidden: bool = False,
) -> set[str]:
    """Every runnable ``nova …`` command path."""
    walked = walk_commands(app, root_name=root_name, include_hidden=include_hidden)
    return {path for path, _ in walked}


def top_level_command_names(
    app: typer.Typer | None = None,
    *,
    include_hidden: bool = False,
) -> set[str]:
    """Names directly under the root, groups included.

    Unlike :func:`command_paths` this does not descend: ``seal`` appears once
    rather than as each of its sub-commands. Used by the CLI-reference gate,
    which documents top-level commands.
    """
    root = root_command(app)
    children = subcommands(root) or {}
    return {
        name
        for name, child in children.items()
        if include_hidden or not getattr(child, "hidden", False)
    }
