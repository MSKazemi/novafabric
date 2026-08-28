"""The ``nova serve`` startup panel must print resolved paths, not a guessed literal.

The panel used to hardcode ``~/.novafabric/.serve-token`` and
``~/.novafabric/registry.db``. Both are wrong whenever ``NOVAFABRIC_HOME`` or
``NOVAFABRIC_DB_PATH`` is set — which is the normal configuration on the
development machine, where the real files live under
``$NOVAFABRIC_HOME``. The token line matters most: it is the *recovery*
instruction. Someone whose dashboard link does not work is told to read a token
out of a file that does not exist at the path they were given, so the one
documented way to recover from a bad link sends them to an empty directory.

These tests assert the panel names the paths the process actually used.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


@pytest.fixture()
def nova_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every NovaFabric path at a tmp dir, as a real operator's env does."""
    home = tmp_path / "nova-home"
    home.mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.delenv("NOVAFABRIC_DB_PATH", raising=False)
    monkeypatch.delenv("NOVAFABRIC_SERVE_TOKEN", raising=False)
    return home


def _panel(nova_home: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run ``nova serve`` up to the point uvicorn would block, return the panel."""
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    result = runner.invoke(
        app, ["serve", "--experimental", "--no-browser", "--port", "4399"]
    )
    assert result.exit_code == 0, result.output
    # Rich wraps the panel at the terminal width, so a long path or token is
    # split across lines. Strip ALL whitespace: the panel contains no path or
    # token with a space in it, so this reassembles them without gluing two
    # separate words into a false match inside a path.
    return re.sub(r"\s+", "", result.output.replace("│", ""))


def test_panel_names_the_token_file_that_was_actually_written(
    nova_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = _panel(nova_home, monkeypatch)
    written = nova_home / ".serve-token"
    assert written.exists(), "serve must write the token file it advertises"
    # The panel must point at the real file, not at the ~/.novafabric guess.
    assert str(written) in text, f"panel does not name {written}:\n{text}"
    assert "~/.novafabric/.serve-token" not in text


def test_panel_names_the_resolved_registry_path(
    nova_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = _panel(nova_home, monkeypatch)
    assert str(nova_home / "registry.db") in text
    assert "~/.novafabric/registry.db" not in text


def test_the_advertised_token_file_contains_the_advertised_token(
    nova_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery path must actually work: file contents == URL token."""
    text = _panel(nova_home, monkeypatch)
    on_disk = (nova_home / ".serve-token").read_text().strip()
    assert on_disk, "token file is empty"
    assert f"token={on_disk}" in text, (
        "the token in the printed URL is not the token on disk — following the "
        f"panel's own recovery instruction would not work:\n{text}"
    )
