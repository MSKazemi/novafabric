"""``verify_seal_dir`` reads every ``.seal/`` file without following a link.

``verify_capsule`` already refused a linked ``manifest.dsse`` (it reads the
envelope itself through ``_capsule_fs``), but ``verify_seal_dir`` — what it and
``nova verify`` call for the NovaSeal checks — opened ``manifest.dsse``,
``manifest.dsse.tsr`` and ``log-entry.json`` with ``Path.read_bytes()``. A capsule
whose token or log entry was a symlink (to identical bytes elsewhere, or to any
file the verifier can read) verified exactly like one that carried them, and a
FIFO in their place hung the verifier. Each file is now read through
``novafabric._capsule_fs`` with a size bound; a link or special file is a
failure, never followed and never waited on.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from novafabric.trust.novaseal import (
    LOG_INCLUSION_FAILED,
    KeyConfig,
    NovaSeal,
    verify_seal_dir,
)
from novafabric.trust.novaseal.capsule_verify import verify_capsule

from ._tsa_pki import TokenSpec, make_tsa_cert, make_tsr
from ._x509_pki import make_cert, make_pki


@pytest.fixture()
def sealed(tmp_path: Path) -> Path:
    """A real seal with a real RFC 3161 token; returns the capsule dir."""
    pki = make_pki("seal-links")
    key_path, cert_path = tmp_path / "seal.key", tmp_path / "seal.crt"
    key_path.write_bytes(pki.leaf.key_pem)
    cert_path.write_bytes(pki.leaf.cert_pem)
    seal = NovaSeal(
        config=KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path)),
        tsa_url="",
        db_path=str(tmp_path / "merkle.db"),
    )
    bundle = seal.seal({"run_id": "seal-links-001", "status": "success"})
    seal.__dict__["_merkle"].close()
    seal_dir = tmp_path / "capsule" / ".seal"
    seal_dir.mkdir(parents=True)
    (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal_dir / "log-entry.json").write_text(json.dumps(bundle.log_entry), encoding="utf-8")
    tsa = make_tsa_cert(make_cert("links-root", issuer=None, ca=True), cn="links-tsa")
    token = make_tsr(tsa, TokenSpec(digest=hashlib.sha256(bundle.dsse_envelope).digest()))
    (seal_dir / "manifest.dsse.tsr").write_bytes(token)
    return seal_dir.parent


def _relink(capsule: Path, name: str, tmp_path: Path) -> None:
    """Replace ``.seal/<name>`` with a symlink to an identical copy outside the capsule."""
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    target = outside / name
    shutil.move(str(capsule / ".seal" / name), target)
    (capsule / ".seal" / name).symlink_to(target)


@contextmanager
def _fifo_writer_after(fifo: Path, grace: float = 5.0) -> Iterator[list[bool]]:
    """Play the FIFO's writer after *grace* s so a blocking open fails, not hangs."""
    stop = threading.Event()
    blocked: list[bool] = []

    def _unblock() -> None:
        while not stop.wait(grace):
            try:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
                blocked.append(True)
            except OSError:
                pass

    t = threading.Thread(target=_unblock, daemon=True)
    t.start()
    try:
        yield blocked
    finally:
        stop.set()
        t.join(10)


def test_baseline_real_seal_verifies(sealed: Path) -> None:
    result = verify_seal_dir(sealed / ".seal")
    assert result.valid, result.errors
    assert result.timestamp_ok is True


def test_linked_log_entry_is_not_followed(sealed: Path, tmp_path: Path) -> None:
    _relink(sealed, "log-entry.json", tmp_path)
    result = verify_seal_dir(sealed / ".seal")
    assert result.valid is False
    assert result.log_integrity_ok is False
    assert result.log_inclusion == LOG_INCLUSION_FAILED
    assert any("symlink" in e for e in result.errors), result.errors


def test_linked_token_is_not_followed(sealed: Path, tmp_path: Path) -> None:
    _relink(sealed, "manifest.dsse.tsr", tmp_path)
    result = verify_seal_dir(sealed / ".seal")
    assert result.valid is False
    assert result.timestamp_ok is False
    assert any("symlink" in e for e in result.errors), result.errors


def test_linked_envelope_is_not_followed(sealed: Path, tmp_path: Path) -> None:
    _relink(sealed, "manifest.dsse", tmp_path)
    result = verify_seal_dir(sealed / ".seal")
    assert result.valid is False
    assert result.signature_ok is False
    assert any("symlink" in e for e in result.errors), result.errors


def test_linked_seal_dir_is_not_followed(sealed: Path, tmp_path: Path) -> None:
    real = tmp_path / "real-seal"
    shutil.move(str(sealed / ".seal"), real)
    (sealed / ".seal").symlink_to(real)
    result = verify_seal_dir(sealed / ".seal")
    assert result.valid is False
    assert result.signature_ok is False


@pytest.mark.parametrize("name", ["log-entry.json", "manifest.dsse.tsr"])
def test_verify_capsule_fails_a_linked_seal_file(sealed: Path, tmp_path: Path, name: str) -> None:
    _relink(sealed, name, tmp_path)
    report = verify_capsule(sealed, None)
    assert report.valid is False


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
@pytest.mark.parametrize("name", ["log-entry.json", "manifest.dsse.tsr", "manifest.dsse"])
def test_fifo_seal_file_never_blocks(sealed: Path, name: str) -> None:
    fifo = sealed / ".seal" / name
    fifo.unlink()
    os.mkfifo(fifo)
    with _fifo_writer_after(fifo) as blocked:
        result = verify_seal_dir(sealed / ".seal")
    assert not blocked, "verify_seal_dir blocked opening a FIFO"
    assert result.valid is False


def test_oversized_log_entry_is_refused(sealed: Path) -> None:
    log_entry = sealed / ".seal" / "log-entry.json"
    record = json.loads(log_entry.read_text())
    record["padding"] = " " * (5 * 1024 * 1024)
    log_entry.write_text(json.dumps(record))
    result = verify_seal_dir(sealed / ".seal")
    assert result.valid is False
    assert any("larger than" in e for e in result.errors), result.errors


def test_relative_and_dotted_seal_paths_still_work(
    sealed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(sealed / ".seal")
    assert verify_seal_dir(".").valid
    monkeypatch.chdir(sealed)
    assert verify_seal_dir(".seal").valid
    assert verify_seal_dir(Path(".seal") / ".." / ".seal").valid


def test_missing_seal_dir_reports_missing(tmp_path: Path) -> None:
    result = verify_seal_dir(tmp_path / "nope" / ".seal")
    assert result.valid is False
    assert any("Missing" in e for e in result.errors), result.errors
