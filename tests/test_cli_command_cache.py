"""The CliRunner command cache (tests/_cli_command_cache.py) must be invisible.

It reuses the click command typer builds for an app, keyed on a fingerprint of the
app's registrations. These guards pin that it is active, that a cached command
behaves exactly like a fresh build, and that every kind of registration change a
test makes gets a fresh build.
"""

from __future__ import annotations

import os

import _cli_command_cache
import pytest
import typer
import typer.testing
from typer.testing import CliRunner

pytestmark = pytest.mark.skipif(
    os.environ.get("NOVA_TEST_CLI_CACHE", "").lower() == "off", reason="cache turned off"
)


def test_the_cache_is_installed() -> None:
    assert getattr(typer.testing._get_command, "_nova_cached", False)


def test_a_cached_command_renders_what_a_fresh_build_renders(monkeypatch) -> None:
    from novafabric.cli.main import app

    runner = CliRunner()
    first = runner.invoke(app, ["--help"])
    cached = runner.invoke(app, ["--help"])
    monkeypatch.setattr(typer.testing, "_get_command", typer.testing._get_command._nova_build)
    fresh = runner.invoke(app, ["--help"])
    assert first.exit_code == cached.exit_code == fresh.exit_code == 0
    assert cached.output == fresh.output == first.output


def test_a_second_invoke_reuses_the_build() -> None:
    app = typer.Typer()

    @app.command()
    def hello() -> None:
        typer.echo("hi")

    runner = CliRunner()
    builds = _cli_command_cache._stats["builds"]
    hits = _cli_command_cache._stats["hits"]
    assert runner.invoke(app, []).output == "hi\n"
    assert runner.invoke(app, []).output == "hi\n"
    assert _cli_command_cache._stats["builds"] == builds + 1
    assert _cli_command_cache._stats["hits"] == hits + 1


def test_adding_a_command_gets_a_fresh_build() -> None:
    app = typer.Typer()

    @app.command()
    def one() -> None:
        typer.echo("one")

    @app.command()
    def two() -> None:
        typer.echo("two")

    runner = CliRunner()
    assert runner.invoke(app, ["one"]).exit_code == 0

    @app.command()
    def three() -> None:
        typer.echo("three")

    result = runner.invoke(app, ["three"])
    assert result.exit_code == 0, result.output
    assert result.output == "three\n"


def test_replacing_a_registered_callback_gets_a_fresh_build() -> None:
    app = typer.Typer()

    @app.command()
    def greet() -> None:
        typer.echo("old")

    @app.command()
    def other() -> None:
        pass

    runner = CliRunner()
    assert runner.invoke(app, ["greet"]).output == "old\n"

    def replacement() -> None:
        typer.echo("new")

    info = next(c for c in app.registered_commands if c.callback is greet)
    info.callback = replacement
    info.name = "greet"
    assert runner.invoke(app, ["greet"]).output == "new\n"


def test_a_sub_app_change_gets_a_fresh_build() -> None:
    app = typer.Typer()
    sub = typer.Typer()
    app.add_typer(sub, name="sub")

    @sub.command()
    def a() -> None:
        typer.echo("a")

    @sub.command()
    def b() -> None:
        typer.echo("b")

    runner = CliRunner()
    assert runner.invoke(app, ["sub", "a"]).output == "a\n"

    @sub.command()
    def c() -> None:
        typer.echo("c")

    assert runner.invoke(app, ["sub", "c"]).output == "c\n"
