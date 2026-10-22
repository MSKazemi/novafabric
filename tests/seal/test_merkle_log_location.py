"""Where the NovaSeal Merkle log lives — one answer for every caller (audit H2, M2).

H2: ``merkle_db: postgres://user:pass@host/db`` was wrapped in ``Path()``, which
collapses ``//`` to ``/``. The loader then ``mkdir``-ed the result under the
current directory — a directory tree named after the DSN, password included —
and capture sealing, seeing ``postgres:/…`` instead of ``postgres://…``, silently
opened a SQLite file there.

M2: capture sealing, serve sealing, ``nova verify`` and the radar read
``profile.merkle_db``, which ignored ``NOVAFABRIC_SEAL_DB_PATH``; the SealTab,
``server/routes/seal.py`` and ``nova seal log verify`` read
``resolve_merkle_db_uri()``, which honours it first. With the variable set, the
log a capsule was sealed into was not the log anything verified against, and
``nova seal init --force`` logged key rotations into a third place.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.capture.finalize import evidence_digests, seal_capsule
from novafabric.cli.main import app
from novafabric.trust.novaseal.capsule_verify import verify_capsule
from novafabric.trust.novaseal.config import (
    SealConfigError,
    load_signing_profile,
    resolve_merkle_db_path,
    resolve_merkle_db_uri,
)
from novafabric.trust.novaseal.merkle import PostgresMerkleLog, open_merkle_log

runner = CliRunner()
SECRET = "s3cr3t-pw"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "nova-home"
    monkeypatch.setenv("NOVAFABRIC_HOME", str(root))
    monkeypatch.delenv("NOVAFABRIC_SEAL_CONFIG", raising=False)
    monkeypatch.delenv("NOVAFABRIC_SEAL_DB_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    return root


def _seal_init(home: Path, *args: str) -> None:
    result = runner.invoke(app, ["seal", "init", "--home", str(home), *args])
    assert result.exit_code == 0, result.output


def _set_merkle_db(home: Path, value: str) -> None:
    config = home / "novaseal.yaml"
    raw = yaml.safe_load(config.read_text())
    raw["merkle_db"] = value
    # keep the managed marker line so `nova seal init` still owns the file
    marker = config.read_text().splitlines()[0]
    config.write_text(marker + "\n" + yaml.safe_dump(raw))


def _tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def _leaves(db: Path) -> list[dict[str, object]]:
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT entry_json FROM leaves ORDER BY leaf_index").fetchall()
    finally:
        conn.close()
    return [json.loads(r[0]) for r in rows]


def _capsule(home: Path, name: str) -> tuple[Path, dict[str, object]]:
    capsule = home / "capsules" / name
    capsule.mkdir(parents=True)
    (capsule / "trace.jsonl").write_text('{"span":"root"}\n')
    manifest: dict[str, object] = {"run_id": name, "status": "success"}
    manifest["evidence_digests"] = evidence_digests(capsule)
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    return capsule, manifest


# ---------------------------------------------------------------------------
# H2 — a DSN stays a DSN
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (f"postgres://nova:{SECRET}@127.0.0.1:1/seal", f"postgres://nova:{SECRET}@127.0.0.1:1/seal"),
        (
            f"postgresql://nova:{SECRET}@127.0.0.1:1/seal",
            f"postgresql://nova:{SECRET}@127.0.0.1:1/seal",
        ),
        (
            f"postgresql+psycopg://nova:{SECRET}@127.0.0.1:1/seal",
            f"postgresql://nova:{SECRET}@127.0.0.1:1/seal",
        ),
    ],
)
def test_a_postgres_merkle_db_is_kept_as_a_dsn_and_never_touches_disk(
    home: Path, tmp_path: Path, configured: str, expected: str
) -> None:
    _seal_init(home)
    _set_merkle_db(home, configured)
    before = _tree(tmp_path)

    profile = load_signing_profile()
    assert profile is not None
    assert profile.merkle_db == expected
    assert resolve_merkle_db_uri() == expected
    log = open_merkle_log(profile.merkle_db)
    try:
        assert isinstance(log, PostgresMerkleLog)
    finally:
        log.close()

    assert _tree(tmp_path) == before, "loading the profile created files"
    assert not any("postgres" in part for part in _tree(tmp_path))


def test_capture_sealing_with_a_dsn_uses_postgres_not_a_sqlite_file(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("psycopg")
    _seal_init(home)
    _set_merkle_db(home, f"postgres://nova:{SECRET}@127.0.0.1:1/seal")
    capsule, manifest = _capsule(home, "01H2DSNCAPSULE000000000000")
    before = _tree(tmp_path)

    reason = seal_capsule(capsule, manifest)

    # Port 1 refuses: sealing degrades (never blocks the workload) — but it went
    # to Postgres, created nothing on disk, and never printed the password.
    assert reason is not None
    assert SECRET not in reason
    assert SECRET not in capsys.readouterr().err
    assert _tree(tmp_path) == before
    assert not (capsule / ".seal").exists()


@pytest.mark.parametrize(
    "configured",
    [
        f"postgresql+psycopg2://nova:{SECRET}@pghost/seal",
        f"mysql://nova:{SECRET}@dbhost/seal",
        f"postgres:/nova:{SECRET}@pghost/seal",
    ],
)
def test_an_unusable_merkle_db_url_is_a_config_error_without_the_password(
    home: Path, tmp_path: Path, configured: str
) -> None:
    _seal_init(home)
    _set_merkle_db(home, configured)
    before = _tree(tmp_path)
    with pytest.raises(SealConfigError) as excinfo:
        load_signing_profile()
    assert SECRET not in str(excinfo.value)
    assert _tree(tmp_path) == before


def test_resolve_merkle_db_path_refuses_a_dsn_without_echoing_it(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", f"postgresql://nova:{SECRET}@pghost/seal")
    with pytest.raises(ValueError) as excinfo:
        resolve_merkle_db_path()
    assert SECRET not in str(excinfo.value)
    assert "pghost" in str(excinfo.value)


# ---------------------------------------------------------------------------
# M2 — one resolver
# ---------------------------------------------------------------------------


def test_every_caller_resolves_the_same_log(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seal_init(home)
    yaml_db = tmp_path / "from-yaml" / "merkle.db"
    env_db = tmp_path / "from-env" / "merkle.db"
    _set_merkle_db(home, str(yaml_db))

    profile = load_signing_profile()
    assert profile is not None
    assert str(profile.merkle_db) == resolve_merkle_db_uri() == str(resolve_merkle_db_path())
    assert resolve_merkle_db_uri() == str(yaml_db)

    monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", str(env_db))
    profile = load_signing_profile()
    assert profile is not None
    assert str(profile.merkle_db) == resolve_merkle_db_uri() == str(resolve_merkle_db_path())
    assert resolve_merkle_db_uri() == str(env_db)


def test_the_log_capture_seals_into_is_the_log_verify_checks(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seal_init(home)
    env_db = tmp_path / "from-env" / "merkle.db"
    monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", str(env_db))
    capsule, manifest = _capsule(home, "01M2ENVCAPSULE000000000000")

    assert seal_capsule(capsule, manifest) is None
    assert env_db.is_file(), "capture sealed into a log other than NOVAFABRIC_SEAL_DB_PATH"
    assert not (home / "novaseal-merkle.db").exists()

    report = verify_capsule(capsule, load_signing_profile())
    assert report.valid, report.all_errors()
    assert report.result.log_inclusion == "local-log"

    log_verify = runner.invoke(app, ["seal", "log", "verify"])
    assert log_verify.exit_code == 0, log_verify.output


def test_seal_init_rotation_logs_into_the_resolved_log(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seal_init(home)
    env_db = tmp_path / "from-env" / "merkle.db"
    monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", str(env_db))

    _seal_init(home, "--force")

    assert [e["event"] for e in _leaves(env_db)] == ["key_rotation"]
    assert not (home / "novaseal-merkle.db").exists()


def test_seal_init_rotation_keeps_a_configured_merkle_db(
    home: Path, tmp_path: Path
) -> None:
    _seal_init(home)
    custom = tmp_path / "shared" / "merkle.db"
    _set_merkle_db(home, str(custom))

    _seal_init(home, "--force")

    profile = load_signing_profile()
    assert profile is not None
    assert str(profile.merkle_db) == str(custom), "rotation reset merkle_db to the default"
    assert [e["event"] for e in _leaves(custom)] == ["key_rotation"]


def test_no_secret_is_written_by_seal_init_into_its_messages(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seal_init(home)
    monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", f"postgresql://nova:{SECRET}@127.0.0.1:1/s")
    result = runner.invoke(app, ["seal", "init", "--home", str(home), "--force"])
    assert SECRET not in result.output
    assert not any(SECRET in p.name for p in Path(os.getcwd()).rglob("*"))


def test_serve_doctor_reports_the_resolved_log_and_redacts_a_dsn(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from novafabric.serve.app import create_app

    token = "test-token-1234567890abcdef"
    api = create_app(
        token=token, capsule_dir=tmp_path / "caps", db_path=tmp_path / "r.db", static_dir=None
    )

    def novaseal_row() -> dict[str, object]:
        with TestClient(api) as client:
            res = client.get(
                "/api/doctor", params={"token": token}, headers={"host": "127.0.0.1:4321"}
            )
        assert res.status_code == 200, res.text
        assert SECRET not in res.text
        return next(c for c in res.json()["checks"] if c["name"] == "novaseal_db")

    env_db = tmp_path / "from-env" / "merkle.db"
    monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", str(env_db))
    assert novaseal_row()["detail"] == str(env_db)

    monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", f"postgresql://nova:{SECRET}@pghost/seal")
    assert "nova:***@pghost" in str(novaseal_row()["detail"])
