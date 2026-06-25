"""Tests for the `nova serve` Typer subcommand (CLI gate, flag handling)."""

from __future__ import annotations

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def test_serve_without_experimental_flag_prints_gate_and_exits_0() -> None:
    """Per ADR-0027 the --experimental flag is mandatory; running without it
    must print the gate banner and exit cleanly so users see the message."""
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 0
    assert "EXPERIMENTAL" in result.output
    assert "--experimental" in result.output


def test_serve_help_describes_experimental_flag() -> None:
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "--experimental" in result.output


def test_serve_refuses_non_localhost_bind_without_insecure() -> None:
    """Binding to a non-loopback interface must require --insecure."""
    result = runner.invoke(app, ["serve", "--experimental", "--host", "0.0.0.0"])
    assert result.exit_code == 2
    assert "Refusing to bind" in result.output


def test_serve_command_listed_in_top_level_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "serve" in result.output
