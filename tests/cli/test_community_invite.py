"""The first-run community invitation (T36 #1): shown once, on a terminal, never piped.

Invariants under test:

* ``nova capture`` prints the invitation for the *first* capsule in a directory only.
* Nothing is printed when output is not an interactive terminal.
* ``NOVAFABRIC_COMMUNITY_HINT=0`` silences it.
* ``nova --version`` keeps stdout to exactly one line; the invitation goes to stderr,
  and only when stderr is a terminal.
* No network: the module imports nothing that can open a socket.
"""
from __future__ import annotations

import ast
import io
import sys
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

import novafabric.cli._community as community
import novafabric.cli.capture as capture_mod
from novafabric.cli._community import DISCUSSIONS_URL, HINT_ENV, is_first_capsule
from novafabric.cli.main import app

runner = CliRunner()


def _capture(out: Path) -> str:
    result = runner.invoke(
        app, ["capture", "--output-dir", str(out), sys.executable, "-c", "pass"]
    )
    assert result.exit_code == 0, result.output
    return result.output


@pytest.fixture
def tty_console(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """Swap capture's console for one that reports itself as a terminal."""
    buf = io.StringIO()
    monkeypatch.setattr(
        capture_mod, "console", Console(file=buf, force_terminal=True, color_system=None, width=200)
    )
    monkeypatch.delenv(HINT_ENV, raising=False)
    return buf


def _make_capsule(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "capsule.yaml").write_text("run_id: x\n", encoding="utf-8")
    return d


# ── is_first_capsule ────────────────────────────────────────────────────────


def test_only_capsule_is_first(tmp_path: Path) -> None:
    assert is_first_capsule(_make_capsule(tmp_path, "a"))


def test_second_capsule_is_not_first(tmp_path: Path) -> None:
    _make_capsule(tmp_path, "a")
    assert not is_first_capsule(_make_capsule(tmp_path, "b"))


def test_non_capsule_siblings_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "replays").mkdir()
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    assert is_first_capsule(_make_capsule(tmp_path, "a"))


def test_unreadable_parent_is_not_first(tmp_path: Path) -> None:
    assert not is_first_capsule(tmp_path / "missing" / "a")


# ── nova capture ────────────────────────────────────────────────────────────


def test_first_capture_on_a_terminal_invites(tmp_path: Path, tty_console: io.StringIO) -> None:
    _capture(tmp_path / "runs")
    shown = tty_console.getvalue()
    assert DISCUSSIONS_URL in shown
    assert f"{HINT_ENV}=0" in shown


def test_second_capture_does_not_invite(tmp_path: Path, tty_console: io.StringIO) -> None:
    _capture(tmp_path / "runs")
    tty_console.truncate(0)
    tty_console.seek(0)
    _capture(tmp_path / "runs")
    assert "Capsule written" in tty_console.getvalue()
    assert DISCUSSIONS_URL not in tty_console.getvalue()


def test_opt_out_silences_capture(
    tmp_path: Path, tty_console: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HINT_ENV, "0")
    _capture(tmp_path / "runs")
    assert DISCUSSIONS_URL not in tty_console.getvalue()


def test_failed_capture_does_not_invite(tmp_path: Path, tty_console: io.StringIO) -> None:
    result = runner.invoke(
        app,
        ["capture", "--output-dir", str(tmp_path / "runs"),
         sys.executable, "-c", "import sys; sys.exit(3)"],
    )
    assert result.exit_code == 3
    assert DISCUSSIONS_URL not in tty_console.getvalue()


def test_non_terminal_capture_does_not_invite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(HINT_ENV, raising=False)
    assert DISCUSSIONS_URL not in _capture(tmp_path / "runs")


# ── nova --version ──────────────────────────────────────────────────────────


class _TtyStream(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_version_stdout_stays_one_line_when_piped() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip().count("\n") == 0
    assert DISCUSSIONS_URL not in result.output


def test_version_invites_on_stderr_when_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from novafabric.cli import main

    fake_err = _TtyStream()
    monkeypatch.setattr(main.sys, "stderr", fake_err)
    monkeypatch.delenv(HINT_ENV, raising=False)
    monkeypatch.setattr(main.typer, "echo", lambda msg, err=False: (
        fake_err if err else sys.stdout).write(f"{msg}\n"))
    with pytest.raises(main.typer.Exit):
        main._version_callback(True)
    assert DISCUSSIONS_URL in fake_err.getvalue()
    assert DISCUSSIONS_URL not in capsys.readouterr().out


def test_version_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    from novafabric.cli import main

    fake_err = _TtyStream()
    monkeypatch.setattr(main.sys, "stderr", fake_err)
    monkeypatch.setenv(HINT_ENV, "0")
    with pytest.raises(main.typer.Exit):
        main._version_callback(True)
    assert DISCUSSIONS_URL not in fake_err.getvalue()


# ── pull, never ping ────────────────────────────────────────────────────────


def test_module_imports_nothing_networked() -> None:
    tree = ast.parse(Path(community.__file__).read_text(encoding="utf-8"))
    imported = {
        (n.module or "") if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in n.names
    }
    banned = {"socket", "http", "urllib", "requests", "httpx", "webbrowser"}
    assert not {m.split(".")[0] for m in imported} & banned, imported
