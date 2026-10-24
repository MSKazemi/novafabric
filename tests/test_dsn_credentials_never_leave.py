"""A database password never reaches argv or the console (review storage-backup-09/-10).

``pg_dump``/``pg_restore`` received the full DSN as ``--dbname``, and process argv is
world-readable (``/proc/<pid>/cmdline``, ``ps -ef``) for the whole dump or restore.
``nova db migrate-to-postgres`` printed a keyword DSN's ``password=`` unchanged and cut a
URL password at its first ``@``, leaking the tail. Four redactors had drifted apart;
:mod:`novafabric._dsn` is now the one place.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from novafabric._dsn import libpq_args, redact_dsn

SECRET = "hun@ter2"


@pytest.mark.parametrize("dsn", [
    f"postgresql://nova:{SECRET.replace('@', '%40')}@db.internal:5432/nova",
    f"postgresql://nova:{SECRET}@db.internal:5432/nova",  # an unescaped @ in the password
    f"host=db.internal user=nova password={SECRET} dbname=nova",
    f"host=db.internal user=nova password='{SECRET} x' dbname=nova",
    f"postgresql://nova@db.internal/nova?password={SECRET.replace('@', '%40')}&sslmode=require",
])
def test_redaction_never_shows_the_password(dsn: str) -> None:
    shown = redact_dsn(dsn)
    assert "hun" not in shown and "ter2" not in shown
    assert shown == "db.internal/nova"


@pytest.mark.parametrize(("dsn", "password"), [
    ("postgresql://nova:hun%40ter2@db.internal:5432/nova", SECRET),
    ("postgresql://nova:hun@ter2@db.internal:5432/nova", SECRET),
    (f"host=db.internal user=nova password={SECRET} dbname=nova", SECRET),
    ("host=db user=nova password='a b\\'c' dbname=nova", "a b'c"),
    ("postgresql://nova@db.internal/nova?password=hun%40ter2&sslmode=require", SECRET),
])
def test_libpq_args_moves_the_password_to_the_environment(dsn: str, password: str) -> None:
    clean, env = libpq_args(dsn)
    assert env == {"PGPASSWORD": password}
    assert "hun" not in clean and "ter2" not in clean and "a b" not in clean
    assert "db" in clean and "nova" in clean


def test_a_dsn_without_a_password_is_unchanged() -> None:
    assert libpq_args("postgresql://nova@db/nova") == ("postgresql://nova@db/nova", {})
    assert libpq_args("host=db dbname=nova") == ("host=db dbname=nova", {})


def test_pg_dump_argv_carries_no_password(tmp_path: Path) -> None:
    from novafabric.backup import create

    seen: dict[str, Any] = {}

    def fake(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["argv"], seen["env"] = argv, kwargs.get("env") or {}
        Path(argv[argv.index("--file") + 1]).write_bytes(b"PGDMP")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with patch.object(create.shutil, "which", return_value="/usr/bin/pg_dump"), \
            patch.object(create.subprocess, "run", side_effect=fake):
        create._run_pg_dump(f"postgresql://nova:{SECRET.replace('@', '%40')}@db/nova", tmp_path / "d")
    assert not any("ter2" in a for a in seen["argv"])
    assert seen["env"]["PGPASSWORD"] == SECRET


def test_the_migration_banner_shows_no_password() -> None:
    from novafabric.metadata_store import cli as ms_cli

    assert "hunter2" not in ms_cli._redact_dsn("host=db user=nova password=hunter2 dbname=nova")
    assert "ss" not in ms_cli._redact_dsn("postgresql://nova:p@ss@db/x")
