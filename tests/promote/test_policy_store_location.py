"""One promotion-policy store for the CLI and the dashboard (policy-store split).

``nova policy sign`` and ``nova seal propose/approve/verify/bypass`` defaulted
``--db`` to ``~/.local/share/novafabric/merkle.db``, while the dashboard SealTab
(``GET /api/seal/policy``, ``POST /api/seal/{id}/verify-sod``) and server mode read
the policy from the resolved NovaSeal Merkle log (``NOVAFABRIC_SEAL_DB_PATH``, then
``merkle_db`` in novaseal.yaml, then ``$NOVAFABRIC_HOME/novaseal-merkle.db``). A
policy signed on the command line was invisible to the dashboard, and the
reverse. Every default now goes through that one resolver; an explicit ``--db``
still wins.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.promote.policy_location import (
    PolicyDbLocationError,
    default_policy_db,
    legacy_policy_db,
)
from novafabric.promote.policy_store import PolicyStore
from novafabric.serve.app import create_app

FIXTURE_KEYS = Path(__file__).parent.parent / "fixtures" / "promote" / "keys"
TOKEN = "test-token-policy-location"
HOST = {"host": "127.0.0.1:4321"}
runner = CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isolated HOME and NOVAFABRIC_HOME; no seal config, no DB override."""
    user_home = tmp_path / "user"
    user_home.mkdir()
    nova = tmp_path / "nova"
    nova.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user_home))
    monkeypatch.setenv("NOVAFABRIC_HOME", str(nova))
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(nova / "audit.jsonl"))
    monkeypatch.delenv("NOVAFABRIC_SEAL_DB_PATH", raising=False)
    monkeypatch.delenv("NOVAFABRIC_SEAL_CONFIG", raising=False)
    yield nova


def _sign(*extra: str) -> object:
    return runner.invoke(
        app,
        [
            "policy", "sign",
            "--key", str(FIXTURE_KEYS / "admin.pem"),
            "--cert", str(FIXTURE_KEYS / "admin_cert.pem"),
            "--proposer-subjects", "proposer",
            "--approver-subjects", "approver",
            *extra,
        ],
    )


def _propose(*extra: str) -> object:
    return runner.invoke(
        app,
        [
            "seal", "propose", "cap-001",
            "--justification", "Ready for production promotion today",
            "--key", str(FIXTURE_KEYS / "proposer.pem"),
            "--cert", str(FIXTURE_KEYS / "proposer_cert.pem"),
            *extra,
        ],
    )


# ── the resolver ───────────────────────────────────────────────────────────


def test_default_is_the_resolved_merkle_log(home: Path) -> None:
    assert default_policy_db() == home / "novaseal-merkle.db"


def test_seal_db_override_wins(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", str(tmp_path / "ci" / "log.db"))
    assert default_policy_db() == tmp_path / "ci" / "log.db"


def test_novaseal_yaml_merkle_db_is_used(home: Path, tmp_path: Path) -> None:
    (home / "novaseal.yaml").write_text(f"merkle_db: {tmp_path / 'cfg.db'}\n")
    assert default_policy_db() == tmp_path / "cfg.db"


def test_a_postgres_log_is_refused_with_a_hint(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", "postgresql://u:secret@db/nova")
    with pytest.raises(PolicyDbLocationError, match="--db") as exc:
        default_policy_db()
    assert "secret" not in str(exc.value)


def test_legacy_location_reported_only_when_present(home: Path) -> None:
    assert legacy_policy_db() is None
    legacy = Path.home() / ".local" / "share" / "novafabric" / "merkle.db"
    legacy.parent.mkdir(parents=True)
    PolicyStore(legacy).close()
    assert legacy_policy_db() == legacy


# ── CLI and dashboard agree ────────────────────────────────────────────────


def test_cli_signed_policy_is_what_the_dashboard_reads(home: Path, tmp_path: Path) -> None:
    result = _sign()
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert (home / "novaseal-merkle.db").is_file()
    assert not (Path.home() / ".local" / "share" / "novafabric" / "merkle.db").exists()

    caps = tmp_path / "runs"
    caps.mkdir()
    client = TestClient(create_app(token=TOKEN, capsule_dir=caps, db_path=tmp_path / "r.db"))
    body = client.get("/api/seal/policy", params={"token": TOKEN}, headers=HOST).json()
    assert body.get("configured") is not False, body
    assert body["version"] == 1

    listed = client.get("/api/policy/list", params={"token": TOKEN}, headers=HOST).json()
    assert listed["policy_db"] == str(home / "novaseal-merkle.db")
    assert [p["version"] for p in listed["signed_policies"]] == [1]


def test_propose_finds_the_policy_sign_wrote(home: Path) -> None:
    assert _sign().exit_code == 0  # type: ignore[attr-defined]
    result = _propose()
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]


def test_explicit_db_still_wins(home: Path, tmp_path: Path) -> None:
    db = tmp_path / "explicit.db"
    assert _sign("--db", str(db)).exit_code == 0  # type: ignore[attr-defined]
    assert db.is_file()
    assert not (home / "novaseal-merkle.db").exists()
    assert _propose("--db", str(db)).exit_code == 0  # type: ignore[attr-defined]
    # Without --db the resolved store has no policy.
    assert _propose().exit_code == 1  # type: ignore[attr-defined]


def test_propose_points_at_a_legacy_policy(home: Path) -> None:
    legacy = Path.home() / ".local" / "share" / "novafabric" / "merkle.db"
    legacy.parent.mkdir(parents=True)
    assert _sign("--db", str(legacy)).exit_code == 0  # type: ignore[attr-defined]
    result = _propose()
    assert result.exit_code == 1
    out = result.output  # type: ignore[attr-defined]
    assert "No promotion policy found" in out
    assert str(legacy) in out.replace("\n", "")
    assert "--db" in out


def test_policy_list_reads_the_resolved_store(home: Path) -> None:
    assert _sign().exit_code == 0  # type: ignore[attr-defined]
    result = runner.invoke(app, ["policy", "list"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "novaseal-merkle.db" in result.output


def test_dashboard_signed_policy_is_what_the_cli_reads(home: Path, tmp_path: Path) -> None:
    caps = tmp_path / "runs"
    caps.mkdir()
    keys = home / "keys"
    keys.mkdir()
    for name in ("admin.pem", "admin_cert.pem"):
        (keys / name).write_bytes((FIXTURE_KEYS / name).read_bytes())
    client = TestClient(create_app(token=TOKEN, capsule_dir=caps, db_path=tmp_path / "r.db"))
    r = client.post(
        "/api/policy/sign",
        params={"token": TOKEN},
        headers=HOST,
        json={
            "key_path": str(keys / "admin.pem"),
            "cert_path": str(keys / "admin_cert.pem"),
            "proposer_subjects": ["proposer"],
            "approver_subjects": ["approver"],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["policy_db"] == str(home / "novaseal-merkle.db")
    assert _propose().exit_code == 0  # type: ignore[attr-defined]


def test_cli_refuses_a_postgres_log_without_db(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", "postgresql://u:secret@db/nova")
    result = _sign()
    assert result.exit_code == 2, result.output  # type: ignore[attr-defined]
    assert isinstance(result.exception, SystemExit)  # type: ignore[attr-defined]
    assert "--db" in result.output  # type: ignore[attr-defined]
    assert "secret" not in result.output  # type: ignore[attr-defined]
