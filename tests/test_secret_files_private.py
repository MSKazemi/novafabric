"""Secrets on disk are 0600 from their first byte and are never written through a planted link.

Line-by-line review Wave 2 (secret material at rest). Several writers created a
secret at the umask mode and narrowed it afterwards (``write_text`` then
``chmod(0o600)``), so for an instant — and for good if the process died in
between — a bearer token, signing key or credential was readable by every local
user; others opened the target ``O_TRUNC`` without ``O_NOFOLLOW``, so a link
planted at the name sent the secret wherever it pointed. ``dek.db`` (raw
per-subject AES keys, compliance-privacy-01) was created at the umask mode, and
SQLite gives its ``-wal``/``-shm`` sidecars the same mode.

Every case runs under umask ``0o022`` (a common default) and checks the final
mode; the "link" cases plant a symlink at the target and check the victim is
untouched. ``tests/test_secret_writes_never_chmod_after.py`` keeps new writers
from reintroducing write-then-chmod.
"""

from __future__ import annotations

import io
import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from novafabric._safe_path import PathEscapeError, ensure_private_file

PRIVATE = 0o600


@pytest.fixture(autouse=True)
def umask_022() -> Iterator[None]:
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


@pytest.fixture
def victim(tmp_path: Path) -> Path:
    path = tmp_path / "victim"
    path.write_text("untouched")
    return path


# --- the primitive ---------------------------------------------------------


def test_ensure_private_file_creates_an_empty_0600_file(tmp_path: Path) -> None:
    path = ensure_private_file(tmp_path / "a" / "secret.db")
    assert path.read_bytes() == b""
    assert _mode(path) == PRIVATE


def test_ensure_private_file_narrows_an_existing_file_and_keeps_its_bytes(tmp_path: Path) -> None:
    path = tmp_path / "secret.db"
    path.write_bytes(b"keep")
    os.chmod(path, 0o644)
    ensure_private_file(path)
    assert (_mode(path), path.read_bytes()) == (PRIVATE, b"keep")


def test_ensure_private_file_refuses_a_link(tmp_path: Path, victim: Path) -> None:
    link = tmp_path / "secret.db"
    link.symlink_to(victim)
    with pytest.raises(PathEscapeError):
        ensure_private_file(link)
    assert _mode(victim) == 0o644


# --- SQLite stores of key material -----------------------------------------


def test_dek_store_database_and_sidecars_are_private(tmp_path: Path) -> None:
    pytest.importorskip("cryptography")
    from novafabric.pii.dek.store import DEKStore

    db = tmp_path / "home" / "dek.db"
    store = DEKStore(db)
    store.get_or_create_dek("subject-1")
    files = [p for p in (db, db.with_name("dek.db-wal"), db.with_name("dek.db-shm")) if p.exists()]
    assert db in files
    assert {p.name: _mode(p) for p in files} == {p.name: PRIVATE for p in files}


def test_dek_store_narrows_a_database_left_world_readable(tmp_path: Path) -> None:
    from novafabric.pii.dek.store import DEKStore

    db = tmp_path / "dek.db"
    DEKStore(db).get_or_create_dek("subject-1")
    os.chmod(db, 0o644)
    DEKStore(db)
    assert _mode(db) == PRIVATE


def test_erasure_queue_database_is_private_from_creation(tmp_path: Path) -> None:
    from novafabric.pii.erasure_queue import ErasureQueue

    db = tmp_path / "erasure-queue.db"
    ErasureQueue(db)
    assert _mode(db) == PRIVATE


# --- bearer tokens and credentials -----------------------------------------


@pytest.mark.parametrize(
    ("module", "path_fn"),
    [
        ("novafabric.serve.auth", "serve_token_path"),
        ("novafabric.server.local_token", "server_token_path"),
    ],
)
def test_server_token_file_is_private_and_never_written_through_a_link(
    module: str, path_fn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, victim: Path
) -> None:
    import importlib

    mod = importlib.import_module(module)
    target = tmp_path / ".token"
    monkeypatch.setattr(mod, path_fn, lambda: target)
    target.symlink_to(victim)

    path = mod.write_token_file("tok-123")

    assert path == target and not target.is_symlink()
    assert target.read_text() == "tok-123\n"
    assert _mode(target) == PRIVATE
    assert victim.read_text() == "untouched"


def test_login_credentials_are_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from novafabric.cli import login

    monkeypatch.setattr(login, "_CRED_DIR", tmp_path / "cfg")
    monkeypatch.setattr(login, "_CRED_FILE", tmp_path / "cfg" / "credentials.json")
    login._save_credentials({"https://nova.example": {"access_token": "t"}})
    assert _mode(tmp_path / "cfg" / "credentials.json") == PRIVATE
    assert json.loads((tmp_path / "cfg" / "credentials.json").read_text())


def test_serve_audit_file_is_private_from_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.serve import audit

    target = tmp_path / "dashboard-audit.jsonl"
    monkeypatch.setattr(audit, "_path", lambda: target)
    modes_at_open: list[int] = []
    real_open = Path.open

    def spy_open(self: Path, *args: object, **kwargs: object) -> object:
        if self == target:
            modes_at_open.append(_mode(self))  # mode at the moment the append opens it
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", spy_open)
    audit.append(action="test", args={}, cli_equivalent="nova test", actor_token_fp="fp")
    assert modes_at_open == [PRIVATE]
    assert _mode(target) == PRIVATE


# --- signing keys ----------------------------------------------------------


def test_keyring_key_is_private_and_created_exclusively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.trust import keyring

    monkeypatch.setattr(keyring, "_KEYRING_DIR", tmp_path / "keyring")
    _, fp = keyring.ensure_keypair("alice")
    key = tmp_path / "keyring" / "alice.pem"
    assert _mode(key) == PRIVATE
    assert keyring.ensure_keypair("alice")[1] == fp  # read back, never regenerated


def test_keyring_key_race_reads_the_winners_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.trust import keyring

    monkeypatch.setattr(keyring, "_KEYRING_DIR", tmp_path / "keyring")
    _, winner_fp = keyring.ensure_keypair("bob")
    key = tmp_path / "keyring" / "bob.pem"
    real_exists = Path.exists
    # Lose the race: the existence check says "absent", but a key is already there.
    monkeypatch.setattr(Path, "exists", lambda self: False if self == key else real_exists(self))
    assert keyring.ensure_keypair("bob")[1] == winner_fp


def test_offline_token_signing_key_is_private_and_never_written_through_a_link(
    tmp_path: Path, victim: Path
) -> None:
    from novafabric.server.offline_tokens import generate_keypair

    key = tmp_path / "keys" / "offline.pem"
    key.parent.mkdir()
    key.symlink_to(victim)
    priv, pub = generate_keypair(key)
    assert not priv.is_symlink() and _mode(priv) == PRIVATE
    assert victim.read_text() == "untouched"
    assert b"PUBLIC KEY" in pub.read_bytes()


def test_nova_init_signing_key_is_private(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from novafabric.cli.main import app

    result = CliRunner().invoke(app, ["init", "--home", str(tmp_path / "home")])
    assert result.exit_code == 0, result.output
    keys = tmp_path / "home" / "keys"
    (private,) = [p for p in keys.iterdir() if p.is_file() and not p.name.endswith(".pub.pem")]
    assert _mode(private) == PRIVATE


# --- NovaSeal forward-secure ratchet ---------------------------------------


def test_ratchet_state_is_private(tmp_path: Path) -> None:
    from novafabric.trust.novaseal import ratchet

    ratchet.init_ratchet("node-1", state_dir=tmp_path)
    assert _mode(tmp_path / "node-1.json") == PRIVATE


def test_ratchet_rotation_that_fails_midway_keeps_the_current_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.trust.novaseal import ratchet

    before = ratchet.init_ratchet("node-2", state_dir=tmp_path)

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(ratchet, "write_private_file", boom)
    with pytest.raises(OSError):
        ratchet.rotate("node-2", state_dir=tmp_path)
    assert ratchet.load_state("node-2", tmp_path) == before  # still signable, not erased


def test_ratchet_rotation_erases_the_previous_chain_key_bytes(tmp_path: Path) -> None:
    from novafabric.trust.novaseal import ratchet

    before = ratchet.init_ratchet("node-3", state_dir=tmp_path)
    old_inode = tmp_path / "old-inode"
    os.link(tmp_path / "node-3.json", old_inode)  # stands in for the freed blocks

    after = ratchet.rotate("node-3", state_dir=tmp_path)

    assert after.epoch == before.epoch + 1
    assert before.chain_key_hex.encode() not in old_inode.read_bytes()
    assert _mode(tmp_path / "node-3.json") == PRIVATE


# --- backup restore ---------------------------------------------------------


def test_restored_sensitive_member_is_private_from_its_first_byte(tmp_path: Path) -> None:
    from novafabric.backup import restore

    dst = tmp_path / "keys" / "evidence-key.pem"
    dst.parent.mkdir()
    # The header is split so a secret scanner does not read a bare marker as a key.
    restore._write_member_bytes(io.BytesIO(b"-----BEGIN PRIVATE" + b" KEY-----"), dst, mode=PRIVATE)
    assert _mode(dst) == PRIVATE
