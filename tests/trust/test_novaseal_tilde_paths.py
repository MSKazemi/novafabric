"""Audit L28 — a ``~`` in a NovaSeal log location means the home directory.

``MerkleLog(Path("~/x/log.db"))`` ran ``db_path.parent.mkdir(...)`` on the
unexpanded path and created a literal ``./~`` directory in the current working
directory; ``NovaSeal(..., db_path="~/…")`` reached it through the same class.
The fix lives at the bottom of the stack (``MerkleLog``) and in the shared
``log_location.normalize_location`` resolver, so every caller benefits. A DSN is
never expanded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from novafabric.trust.novaseal import KeyConfig, NovaSeal
from novafabric.trust.novaseal.log_location import normalize_location
from novafabric.trust.novaseal.merkle import (
    MerkleLog,
    open_existing_merkle_log,
    open_merkle_log,
)


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(cwd)
    return home


def _no_literal_tilde(tmp_path: Path) -> None:
    assert not (tmp_path / "cwd" / "~").exists(), "created a literal ./~ directory"


def test_merkle_log_expands_tilde(fake_home: Path, tmp_path: Path) -> None:
    log = MerkleLog(Path("~/seal/log.db"))
    try:
        assert (fake_home / "seal" / "log.db").is_file()
    finally:
        log.close()
    _no_literal_tilde(tmp_path)


@pytest.mark.parametrize("given", ["~/seal/log.db", Path("~/seal/log.db")])
def test_open_merkle_log_expands_tilde(
    fake_home: Path, tmp_path: Path, given: str | Path
) -> None:
    log = open_merkle_log(given)
    try:
        assert (fake_home / "seal" / "log.db").is_file()
    finally:
        log.close()
    _no_literal_tilde(tmp_path)


def test_open_existing_merkle_log_expands_tilde(fake_home: Path, tmp_path: Path) -> None:
    open_merkle_log(fake_home / "seal" / "log.db").close()
    log = open_existing_merkle_log("~/seal/log.db")
    assert log is not None
    log.close()
    _no_literal_tilde(tmp_path)


def test_novaseal_constructor_expands_tilde(fake_home: Path, tmp_path: Path) -> None:
    seal = NovaSeal(
        KeyConfig(profile="local", key_path="~/k.pem", cert_path="~/c.pem"),
        tsa_url="",
        db_path="~/seal/log.db",
    )
    assert (fake_home / "seal" / "log.db").is_file()
    assert seal.__dict__["_key_path"] == fake_home / "k.pem"
    assert seal.__dict__["_cert_path"] == fake_home / "c.pem"
    _no_literal_tilde(tmp_path)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:p@host/db?options=~x",
        "postgres://~user@host/db",
    ],
)
def test_dsn_is_never_expanded(fake_home: Path, dsn: str) -> None:
    assert normalize_location(dsn) == dsn
