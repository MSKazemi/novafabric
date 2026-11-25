"""Reuse the built click command across `CliRunner.invoke(app, ...)` calls.

`typer.testing.CliRunner.invoke` calls `typer.main.get_command(app)` on every
invocation, which rebuilds the whole command tree: measured 0.31 s per call on CI
hardware and 0.46 s on a loaded laptop for `novafabric.cli.main.app` (~1,100
commands), against ~0.02 s for the invocation itself. The suite makes about 1,400
such calls across 300+ test files, so most of the CLI tests' time was spent
rebuilding an identical tree.

The cache is keyed on a fingerprint of the app's registrations: the identity of
every `Typer`, its info, callback, `CommandInfo` and `TyperInfo`, and of each of
their attribute values, recursively. The entry holds those objects, so no id in a
fingerprint can be reused by a new object while the entry exists. A test that adds a command, replaces a
callback or reassigns any registration attribute changes the fingerprint and gets a
freshly built command — exactly what typer would have built. Taking the
fingerprint costs ~5 ms.

What it does not change: `typer.main.get_command` itself (a test that calls it
directly still gets a fresh build), and each invocation's click Context, which
`Command.main` creates per call. `NOVA_TEST_CLI_CACHE=off` turns the cache off.

Guard: tests/test_cli_command_cache.py.
"""

from __future__ import annotations

import os
from typing import Any

# id(app) -> (fingerprint, objects the fingerprint names, built command). The
# objects are held so none of their ids can be reused by a new object while the
# entry exists: an id-based fingerprint is only sound for objects that are alive.
_cache: dict[int, tuple[tuple[int, ...], list[Any], Any]] = {}
_stats = {"hits": 0, "builds": 0}


def _walk(app: Any, out: list[Any], seen: set[int]) -> None:
    if id(app) in seen:
        return
    seen.add(id(app))
    callback = app.registered_callback
    nodes = [app, app.info, *app.registered_commands, *app.registered_groups]
    if callback is not None:
        nodes.append(callback)
    for node in nodes:
        out.append(node)
        try:
            items = sorted(vars(node).items())
        except TypeError:
            continue
        out.extend(v for _k, v in items)
    for group in app.registered_groups:
        if group.typer_instance is not None:
            _walk(group.typer_instance, out, seen)


def fingerprint(app: Any) -> tuple[tuple[int, ...], list[Any]]:
    """(ids, objects) of everything `typer.main.get_command(app)` reads from *app*."""
    objects: list[Any] = []
    _walk(app, objects, set())
    return tuple(id(o) for o in objects), objects


def install() -> None:
    """Route `typer.testing`'s command builder through the cache (idempotent)."""
    if os.environ.get("NOVA_TEST_CLI_CACHE", "").lower() == "off":
        return
    try:
        import typer.testing as typer_testing
    except ImportError:
        return
    if getattr(typer_testing._get_command, "_nova_cached", False):
        return
    build = typer_testing._get_command

    def cached_get_command(app: Any) -> Any:
        key, objects = fingerprint(app)
        hit = _cache.get(id(app))
        if hit is not None and hit[0] == key:
            _stats["hits"] += 1
            return hit[2]
        command = build(app)
        _cache[id(app)] = (key, objects, command)
        _stats["builds"] += 1
        return command

    cached_get_command._nova_cached = True  # type: ignore[attr-defined]
    cached_get_command._nova_build = build  # type: ignore[attr-defined]
    typer_testing._get_command = cached_get_command
