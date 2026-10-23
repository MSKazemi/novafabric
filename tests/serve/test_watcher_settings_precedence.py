"""Audit L27 + L30 — capsule-watcher settings: explicit flag > env > default.

L27: an unknown ``NOVA_WATCHER_BACKEND`` (``watchdgo``) silently fell through to
``auto``. L30: ``NOVA_WATCHER_BACKEND`` / ``NOVA_WATCHER_INTERVAL`` overrode an
explicit ``--backend`` / ``--interval``, and ``nova ingest-capsule --watch``
then printed the *flag* value while running the env value.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from novafabric.cli.ingest_capsule import app as ingest_app
from novafabric.serve.capsule_watcher import (
    CapsuleWatcher,
    WatcherConfigError,
    resolve_watcher_settings,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOVA_WATCHER_BACKEND", raising=False)
    monkeypatch.delenv("NOVA_WATCHER_INTERVAL", raising=False)


# ── resolve_watcher_settings ─────────────────────────────────────────────


def test_defaults() -> None:
    s = resolve_watcher_settings()
    assert (s.backend, s.interval) == ("auto", 2.0)
    assert (s.backend_source, s.interval_source) == ("default", "default")


def test_env_applies_when_no_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_WATCHER_BACKEND", "polling")
    monkeypatch.setenv("NOVA_WATCHER_INTERVAL", "7.5")
    s = resolve_watcher_settings()
    assert (s.backend, s.interval) == ("polling", 7.5)
    assert (s.backend_source, s.interval_source) == ("env", "env")


def test_explicit_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_WATCHER_BACKEND", "watchdog")
    monkeypatch.setenv("NOVA_WATCHER_INTERVAL", "30")
    s = resolve_watcher_settings(backend="polling", interval=0.5)
    assert (s.backend, s.interval) == ("polling", 0.5)
    assert (s.backend_source, s.interval_source) == ("flag", "flag")


def test_backend_is_case_and_space_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_WATCHER_BACKEND", " Polling ")
    assert resolve_watcher_settings().backend == "polling"


@pytest.mark.parametrize("bad", ["watchdgo", "inotify", ""])
def test_unknown_env_backend_is_refused(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("NOVA_WATCHER_BACKEND", bad)
    with pytest.raises(WatcherConfigError, match="NOVA_WATCHER_BACKEND"):
        resolve_watcher_settings()


def test_unknown_flag_backend_is_refused() -> None:
    with pytest.raises(WatcherConfigError, match="--backend"):
        resolve_watcher_settings(backend="watchdgo")


@pytest.mark.parametrize("bad", ["abc", "0", "-1", "nan", "inf"])
def test_bad_env_interval_is_refused(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("NOVA_WATCHER_INTERVAL", bad)
    with pytest.raises(WatcherConfigError, match="NOVA_WATCHER_INTERVAL"):
        resolve_watcher_settings()


def test_bad_flag_interval_is_refused() -> None:
    with pytest.raises(WatcherConfigError, match="--interval"):
        resolve_watcher_settings(interval=0.0)


# ── CapsuleWatcher honours the precedence ────────────────────────────────


def test_watcher_explicit_backend_beats_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_WATCHER_BACKEND", "watchdog")
    monkeypatch.setenv("NOVA_WATCHER_INTERVAL", "30")
    w = CapsuleWatcher(tmp_path, db_path=tmp_path / "r.db", backend="polling", interval=0.25)
    try:
        assert w.backend_name() == "polling"
        assert w.interval == 0.25
    finally:
        w.close()


def test_watcher_env_applies_without_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_WATCHER_BACKEND", "polling")
    monkeypatch.setenv("NOVA_WATCHER_INTERVAL", "9")
    w = CapsuleWatcher(tmp_path, db_path=tmp_path / "r.db")
    assert w.backend_name() == "polling"
    assert w.interval == 9.0


def test_watcher_unknown_env_backend_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_WATCHER_BACKEND", "watchdgo")
    with pytest.raises(WatcherConfigError):
        CapsuleWatcher(tmp_path, db_path=tmp_path / "r.db")


# ── nova ingest-capsule ──────────────────────────────────────────────────


def _watch(tmp_path: Path, *extra: str) -> object:
    caps = tmp_path / "caps"
    caps.mkdir(exist_ok=True)
    with patch("novafabric.serve.capsule_watcher.CapsuleWatcher.poll_once", return_value=0):
        with patch("time.sleep", side_effect=KeyboardInterrupt) as sleep:
            result = CliRunner().invoke(
                ingest_app,
                ["--watch", "--capsule-dir", str(caps), "--db-path", str(tmp_path / "r.db"), *extra],
            )
    return result, sleep


def test_cli_prints_and_uses_the_effective_env_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_WATCHER_INTERVAL", "7.5")
    monkeypatch.setenv("NOVA_WATCHER_BACKEND", "polling")
    result, sleep = _watch(tmp_path)
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert "backend: polling, interval: 7.5 s" in result.output  # type: ignore[attr-defined]
    sleep.assert_called_with(7.5)


def test_cli_flag_beats_env_and_is_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_WATCHER_INTERVAL", "30")
    monkeypatch.setenv("NOVA_WATCHER_BACKEND", "watchdog")
    result, sleep = _watch(tmp_path, "--interval", "0.5", "--backend", "polling")
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert "backend: polling, interval: 0.5 s" in result.output  # type: ignore[attr-defined]
    sleep.assert_called_with(0.5)


def test_cli_refuses_unknown_backend_flag(tmp_path: Path) -> None:
    result, _ = _watch(tmp_path, "--backend", "watchdgo")
    assert result.exit_code == 2  # type: ignore[attr-defined]
    assert isinstance(result.exception, SystemExit)  # type: ignore[attr-defined]
    assert "watchdgo" in result.output  # type: ignore[attr-defined]


def test_cli_refuses_unknown_env_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_WATCHER_BACKEND", "watchdgo")
    result, _ = _watch(tmp_path)
    assert result.exit_code == 2  # type: ignore[attr-defined]
    assert isinstance(result.exception, SystemExit)  # type: ignore[attr-defined]
    assert "NOVA_WATCHER_BACKEND" in result.output  # type: ignore[attr-defined]


# ── nova serve refuses at startup ─────────────────────────────────────────


def test_create_app_refuses_unknown_env_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastapi")
    from novafabric.serve.app import create_app

    monkeypatch.setenv("NOVA_WATCHER_BACKEND", "watchdgo")
    caps = tmp_path / "runs"
    caps.mkdir()
    with pytest.raises(WatcherConfigError, match="NOVA_WATCHER_BACKEND"):
        create_app(token="t" * 24, capsule_dir=caps, db_path=tmp_path / "r.db")


def test_nova_serve_refuses_unknown_env_backend_before_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")
    from novafabric.cli.main import app as nova_app

    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NOVA_WATCHER_BACKEND", "watchdgo")
    caps = tmp_path / "runs"
    caps.mkdir()
    with patch("uvicorn.run") as run:
        result = CliRunner().invoke(
            nova_app,
            ["serve", "--experimental", "--capsule-dir", str(caps), "--no-browser"],
        )
    assert result.exit_code == 2, result.output
    assert isinstance(result.exception, SystemExit)
    assert "NOVA_WATCHER_BACKEND" in result.output
    run.assert_not_called()
