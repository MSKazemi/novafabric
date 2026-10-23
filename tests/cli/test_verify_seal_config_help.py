"""Audit L23 — `nova verify --seal-config` names the real default location.

The help said ``~/.novafabric/novaseal.yaml``; the loader reads
``$NOVAFABRIC_HOME/novaseal.yaml`` (``config._default_config_path``). With
``NOVAFABRIC_HOME`` set elsewhere, a file at the advertised path is not read.
"""

from __future__ import annotations

import re

from _help_assert import strip_ansi
from typer.testing import CliRunner

from novafabric.cli.main import app


def _flat_help() -> str:
    result = CliRunner().invoke(app, ["verify", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    # Collapse rich's box-drawing and wrapping so the phrase is searchable.
    text = strip_ansi(result.output)
    text = re.sub(r"[│╭╮╰╯─]", " ", text)
    return re.sub(r"\s+", " ", text)


def test_help_names_nova_home_default() -> None:
    text = _flat_help()
    assert "$NOVAFABRIC_HOME/novaseal.yaml" in text
    assert "default: ~/.novafabric/novaseal.yaml)" not in text


def test_trust_config_docstring_matches_the_loader() -> None:
    from novafabric.trust.novaseal import config

    assert config.__dict__["_default_config_path"]().name == "novaseal.yaml"
    assert "$NOVAFABRIC_HOME/novaseal.yaml" in (config.__doc__ or "")
