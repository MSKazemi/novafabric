"""Request-named read paths never reach the server's own secrets.

The read roots cover all of ``$NOVAFABRIC_HOME``. That home also holds the
token stores, the signing keys, the PII data-encryption keys, the NovaSeal
ratchet state and the audit log. So an ``operate`` token could name them as a
*source*: ``POST /api/policy/test`` with ``bundle_path=$NOVAFABRIC_HOME`` ran
``opa test`` over the whole home. Reads now go through a deny list
(``serve.paths.secret_paths`` plus key material for source reads). A path that
is, lies inside, or contains a secret is refused with 403. Signing-key fields
(``key_path``/``cert_path``) may still name key material, and nothing else on
the list.

Also here: ``GET /api/assure/{run_id}?capsule_path=`` used ``Path(capsule_path)``
unconfined, which made it an existence oracle for any directory holding a
``capsule.yaml``. It now goes through the same read confinement.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402
from novafabric.serve.paths import (  # noqa: E402
    PATH_ROOTS_ENV,
    PathDeniedError,
    confine_key,
    confine_read,
    secret_paths,
)

TOKEN = "secret-roots-token-0123456789abcdef"
H = {"host": "127.0.0.1:4321", "authorization": f"Bearer {TOKEN}"}
RUN = "01J9ZZSECRETROOTS000000001"


@dataclass
class Env:
    home: Path
    capsules: Path
    outside: Path
    client: TestClient


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    home = tmp_path / "home" / ".novafabric"
    for sub in ("exports", "keys", "seal/ratchet"):
        (home / sub).mkdir(parents=True)
    for rel in (
        "tokens.jsonl",
        ".serve-token",
        ".server-token",
        ".env",
        "dek.db",
        "dek.db-wal",
        "config.yaml",
        "dashboard-audit.jsonl",
        "keys/local-key.pem",
        "seal/ratchet/node-a.json",
        "exports/stray.key",
    ):
        (home / rel).write_text("secret\n")
    capsules = tmp_path / "capsules"
    cap = capsules / RUN
    cap.mkdir(parents=True)
    (cap / "capsule.yaml").write_text(yaml.safe_dump({"run_id": RUN}))
    outside = tmp_path / "outside"
    (outside / "capsule").mkdir(parents=True)
    (outside / "capsule" / "capsule.yaml").write_text(yaml.safe_dump({"run_id": "x"}))
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(home / "dashboard-audit.jsonl"))
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(home / "audit" / "audit.jsonl"))
    monkeypatch.delenv("NOVAFABRIC_RATCHET_DIR", raising=False)
    monkeypatch.delenv("NOVAFABRIC_EVIDENCE_DIR", raising=False)
    monkeypatch.delenv(PATH_ROOTS_ENV, raising=False)
    app = create_app(token=TOKEN, capsule_dir=capsules, db_path=None, static_dir=None)
    with TestClient(app) as c:
        yield Env(home=home, capsules=capsules, outside=outside, client=c)


SECRET_RELS = [
    "tokens.jsonl",
    ".serve-token",
    ".server-token",
    ".env",
    "dek.db",
    "dek.db-wal",
    "config.yaml",
    "dashboard-audit.jsonl",
    "seal/ratchet",
    "seal/ratchet/node-a.json",
    "audit/audit.jsonl",
]


# -- the primitive -------------------------------------------------------------


@pytest.mark.parametrize("rel", SECRET_RELS)
def test_source_read_refuses_every_secret(env: Env, rel: str) -> None:
    with pytest.raises(PathDeniedError):
        confine_read(env.home / rel, env.capsules)


@pytest.mark.parametrize("rel", SECRET_RELS)
def test_key_read_refuses_every_non_key_secret(env: Env, rel: str) -> None:
    with pytest.raises(PathDeniedError):
        confine_key(env.home / rel, env.capsules)


@pytest.mark.parametrize("rel", ["keys", "keys/local-key.pem", "exports/stray.key"])
def test_source_read_refuses_key_material(env: Env, rel: str) -> None:
    with pytest.raises(PathDeniedError):
        confine_read(env.home / rel, env.capsules)


@pytest.mark.parametrize("rel", ["keys/local-key.pem", "exports/stray.key"])
def test_key_read_allows_key_material(env: Env, rel: str) -> None:
    assert confine_key(env.home / rel, env.capsules) == (env.home / rel).resolve()


@pytest.mark.parametrize("candidate", ["", ".", "seal"])
def test_a_directory_that_contains_a_secret_is_refused(env: Env, candidate: str) -> None:
    with pytest.raises(PathDeniedError):
        confine_read(env.home / candidate if candidate else env.home, env.capsules)


def test_a_relative_path_cannot_name_a_secret(env: Env) -> None:
    with pytest.raises(PathDeniedError):
        confine_read("tokens.jsonl", env.capsules)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_a_link_inside_a_root_to_a_secret_is_refused(env: Env) -> None:
    os.symlink(env.home / "tokens.jsonl", env.home / "exports" / "innocent.txt")
    with pytest.raises(PathDeniedError):
        confine_read(env.home / "exports" / "innocent.txt", env.capsules)


def test_ordinary_paths_in_the_roots_still_pass(env: Env) -> None:
    assert confine_read(env.capsules / RUN, env.capsules) == (env.capsules / RUN).resolve()
    assert confine_read(env.home / "exports", env.capsules) == (env.home / "exports").resolve()


def test_an_extra_root_cannot_reopen_the_home(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PATH_ROOTS_ENV, str(env.home.parent))
    with pytest.raises(PathDeniedError):
        confine_read(env.home.parent, env.capsules)


def test_secret_paths_follow_override_env_vars(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    moved = tmp_path / "elsewhere" / "ratchet"
    monkeypatch.setenv("NOVAFABRIC_RATCHET_DIR", str(moved))
    assert moved.resolve() in secret_paths()


# -- through HTTP --------------------------------------------------------------


@pytest.mark.parametrize("rel", ["", "tokens.jsonl", "keys", "seal/ratchet"])
def test_policy_test_cannot_take_a_secret_as_its_bundle(env: Env, rel: str) -> None:
    r = env.client.post(
        "/api/policy/test", headers=H, json={"bundle_path": str(env.home / rel)}
    )
    assert r.status_code == 403, r.text[:200]


@pytest.mark.parametrize("rel", ["keys", ".serve-token", ""])
def test_capsule_migrate_cannot_take_a_secret_as_its_source(env: Env, rel: str) -> None:
    r = env.client.post(
        "/api/capsule-migrate",
        headers=H,
        json={"source": str(env.home / rel), "output": "migrated"},
    )
    assert r.status_code == 403, r.text[:200]
    assert not (env.home / "exports" / "migrated").exists()


def test_evidence_key_path_cannot_name_the_token_store(env: Env) -> None:
    r = env.client.post(
        f"/api/evidence/{RUN}",
        headers=H,
        json={"key_path": str(env.home / "tokens.jsonl"), "confirmed": True},
    )
    assert r.status_code == 403, r.text[:200]


def test_kg_capsule_base_cannot_be_the_home(env: Env) -> None:
    r = env.client.post(
        "/api/kg/ingest-all", headers=H, json={"capsule_base": str(env.home)}
    )
    assert r.status_code == 403, r.text[:200]


# -- /api/assure ----------------------------------------------------------------


def test_assure_capsule_path_outside_the_roots_is_refused(env: Env) -> None:
    r = env.client.get(
        f"/api/assure/{RUN}",
        headers=H,
        params={"capsule_path": str(env.outside / "capsule")},
    )
    assert r.status_code == 403, r.text[:200]


def test_assure_is_not_an_existence_oracle(env: Env) -> None:
    present = env.client.get(
        f"/api/assure/{RUN}", headers=H, params={"capsule_path": str(env.outside / "capsule")}
    )
    absent = env.client.get(
        f"/api/assure/{RUN}", headers=H, params={"capsule_path": str(env.outside / "nothing")}
    )
    assert (present.status_code, present.json()) == (absent.status_code, absent.json())


def test_assure_capsule_path_cannot_name_a_secret(env: Env) -> None:
    r = env.client.get(f"/api/assure/{RUN}", headers=H, params={"capsule_path": str(env.home)})
    assert r.status_code == 403


def test_assure_run_id_cannot_climb_out(env: Env) -> None:
    r = env.client.get("/api/assure/%2E%2E", headers=H)
    assert r.status_code in (400, 404)
    assert r.json().get("backend") != "stub" or "not found" in str(r.json())


def test_assure_still_checks_a_capsule_in_the_roots(env: Env) -> None:
    by_id = env.client.get(f"/api/assure/{RUN}", headers=H)
    by_path = env.client.get(
        f"/api/assure/{RUN}", headers=H, params={"capsule_path": str(env.capsules / RUN)}
    )
    assert by_id.status_code == by_path.status_code == 200
    assert by_id.json().get("reason") is None
    assert by_path.json().get("reason") is None


def test_assure_unknown_run_keeps_its_not_found_shape(env: Env) -> None:
    r = env.client.get("/api/assure/01J9ZZNOSUCHRUN0000000000", headers=H)
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "not found" in r.json()["reason"]
