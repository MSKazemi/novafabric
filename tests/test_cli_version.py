"""Regression test for `nova --version` and `nova -V`.

The CLI must expose a working --version flag so users can answer the
question "which NovaFabric is installed?" without having to import
the package.
"""
from __future__ import annotations

import re

from typer.testing import CliRunner

from novafabric.cli.main import _get_version, app

runner = CliRunner()

VERSION_RE = re.compile(r"^novafabric\s+\S+$")


def test_long_flag_prints_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert VERSION_RE.match(result.output.strip()), result.output


def test_short_flag_prints_version() -> None:
    result = runner.invoke(app, ["-V"])
    assert result.exit_code == 0, result.output
    assert VERSION_RE.match(result.output.strip()), result.output


def test_get_version_returns_non_unknown_when_installed() -> None:
    assert _get_version() != "unknown", (
        "package metadata not found — install (e.g. `uv pip install -e .`) before tests"
    )
