"""An Evidence Bundle export never dereferences a symlink inside the capsule.

``EvidenceBundleBuilder._stage_capsule_copy`` used ``shutil.copytree(capsule, dest)``,
whose default ``symlinks=False`` *follows* links: a symlink placed in a capsule (for
example ``inputs/key -> ~/.ssh/id_rsa``) put the target's bytes into the exported ZIP as
an ordinary file, under ``run-capsule/``, past redaction (the redaction scan ran over the
capture, not over whatever a link points at today), and signed by the run attestation.

NovaFabric never writes a symlink into a capsule, and neither a ZIP entry nor the
capsule Merkle root has a way to say "this is a link". The export therefore refuses a
capsule that holds any symlink — naming each one and whether it leaves the capsule —
before anything is written, and the staging copy re-checks with ``symlinks=True`` so a
link that appears after validation is copied as a link and refused, never followed.
"""

from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.cli.main import app
from novafabric.evidence.bundle import (
    CapsuleSetBundleBuilder,
    CapsuleSymlinkError,
    CapsuleValidationError,
    EvidenceBundleBuilder,
)
from novafabric.evidence.signing import LocalSigner, generate_keypair

SECRET = b"-----BEGIN OPENSSH PRIVATE KEY-----\nnot-a-real-key-but-must-not-leak\n"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")


def _capsule(tmp_path: Path, name: str = "a") -> Path:
    return (
        CaptureOrchestrator(base_dir=tmp_path / f"runs-{name}")
        .run(command=[sys.executable, "-c", "pass"])
        .capsule_dir
    )


@pytest.fixture()
def signer(tmp_path: Path) -> LocalSigner:
    priv, _ = generate_keypair(tmp_path / "keys")
    return LocalSigner(priv)


@pytest.fixture()
def secret(tmp_path: Path) -> Path:
    home = tmp_path / "home" / ".ssh"
    home.mkdir(parents=True)
    key = home / "id_rsa"
    key.write_bytes(SECRET)
    return key


def _zip_contains(zip_path: Path, needle: bytes) -> bool:
    with zipfile.ZipFile(zip_path) as zf:
        return any(needle in zf.read(name) for name in zf.namelist())


def _build_refused(capsule: Path, signer: LocalSigner, out: Path) -> CapsuleSymlinkError:
    with pytest.raises(CapsuleSymlinkError) as info:
        EvidenceBundleBuilder(capsule, signer, out).build()
    assert not out.exists(), "nothing may be written when the export is refused"
    return info.value


# ---------------------------------------------------------------------------
# The leak
# ---------------------------------------------------------------------------


def test_symlink_to_a_file_outside_the_capsule_is_refused(
    tmp_path: Path, signer: LocalSigner, secret: Path
) -> None:
    capsule = _capsule(tmp_path)
    (capsule / "inputs" / "key").symlink_to(secret)
    err = _build_refused(capsule, signer, tmp_path / "out.zip")
    assert "inputs/key" in str(err)
    assert "outside the capsule" in str(err)
    assert isinstance(err, CapsuleValidationError)  # every caller already handles it


def test_symlinked_directory_outside_the_capsule_is_refused(
    tmp_path: Path, signer: LocalSigner, secret: Path
) -> None:
    capsule = _capsule(tmp_path)
    (capsule / "outputs" / "dotssh").symlink_to(secret.parent, target_is_directory=True)
    err = _build_refused(capsule, signer, tmp_path / "out.zip")
    assert "outputs/dotssh" in str(err)


def test_symlinked_required_file_is_refused(
    tmp_path: Path, signer: LocalSigner, secret: Path
) -> None:
    """``lineage.jsonl`` is read straight into ``lineage-subgraph/edges.jsonl``."""
    capsule = _capsule(tmp_path)
    lineage = capsule / "lineage.jsonl"
    lineage.unlink(missing_ok=True)
    lineage.symlink_to(secret)
    err = _build_refused(capsule, signer, tmp_path / "out.zip")
    assert "lineage.jsonl" in str(err)


def test_dangling_symlink_is_refused(tmp_path: Path, signer: LocalSigner) -> None:
    capsule = _capsule(tmp_path)
    (capsule / "inputs" / "gone").symlink_to(tmp_path / "does-not-exist")
    err = _build_refused(capsule, signer, tmp_path / "out.zip")
    assert "inputs/gone" in str(err)


def test_symlink_inside_the_capsule_is_refused_too(tmp_path: Path, signer: LocalSigner) -> None:
    """A bundle has no link representation; the copy would silently duplicate content."""
    capsule = _capsule(tmp_path)
    (capsule / "inputs" / "alias.yaml").symlink_to(Path("..") / "capsule.yaml")
    err = _build_refused(capsule, signer, tmp_path / "out.zip")
    assert "inputs/alias.yaml" in str(err)
    assert "inside the capsule" in str(err)


def test_refusal_comes_before_the_policy_decision(
    tmp_path: Path, signer: LocalSigner, secret: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Up front: no policy/audit event and no read through a link for a refused export.

    ``redaction-proof.json`` is parsed during validation; a link there must be refused
    before it is opened, not after the staging copy.
    """
    capsule = _capsule(tmp_path)
    proof = capsule / "redaction-proof.json"
    outside = tmp_path / "elsewhere.json"
    outside.write_bytes(proof.read_bytes())
    proof.unlink()
    proof.symlink_to(outside)

    def gate_reached(self: EvidenceBundleBuilder) -> None:
        raise AssertionError("policy gate reached for a capsule holding a symlink")

    monkeypatch.setattr(EvidenceBundleBuilder, "_run_policy_gate", gate_reached)
    err = _build_refused(capsule, signer, tmp_path / "out.zip")
    assert "redaction-proof.json" in str(err)


def test_link_planted_after_validation_is_never_followed(
    tmp_path: Path, signer: LocalSigner, secret: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link created between the up-front check and the copy (a race) is still refused."""
    capsule = _capsule(tmp_path)
    original_gate = EvidenceBundleBuilder._run_policy_gate

    def gate_then_plant(self: EvidenceBundleBuilder) -> None:
        original_gate(self)
        (capsule / "inputs" / "late").symlink_to(secret)

    monkeypatch.setattr(EvidenceBundleBuilder, "_run_policy_gate", gate_then_plant)
    out = tmp_path / "out.zip"
    with pytest.raises(CapsuleSymlinkError):
        EvidenceBundleBuilder(capsule, signer, out).build()
    assert not out.exists()


def test_capsule_set_export_refuses_a_symlink_in_any_member(
    tmp_path: Path, signer: LocalSigner, secret: Path
) -> None:
    clean, dirty = _capsule(tmp_path, "clean"), _capsule(tmp_path, "dirty")
    (dirty / "inputs" / "key").symlink_to(secret)
    out = tmp_path / "set.zip"
    with pytest.raises(CapsuleSymlinkError):
        CapsuleSetBundleBuilder([clean, dirty], signer, out).build()
    assert not out.exists()


def test_cli_export_refuses_and_exits_1(tmp_path: Path, secret: Path) -> None:
    capsule = _capsule(tmp_path)
    (capsule / "inputs" / "key").symlink_to(secret)
    priv, _ = generate_keypair(tmp_path / "cli-keys")
    out = tmp_path / "cli.zip"
    result = CliRunner().invoke(
        app, ["export-evidence", str(capsule), "--output", str(out), "--key", str(priv)]
    )
    assert result.exit_code == 1, result.output
    assert "symlink" in result.output
    assert not out.exists()


# ---------------------------------------------------------------------------
# What keeps working
# ---------------------------------------------------------------------------


def test_clean_capsule_still_exports(tmp_path: Path, signer: LocalSigner) -> None:
    capsule = _capsule(tmp_path)
    out = tmp_path / "out.zip"
    EvidenceBundleBuilder(capsule, signer, out).build()
    with zipfile.ZipFile(out) as zf:
        assert "run-capsule/capsule.yaml" in zf.namelist()


def test_capsule_reached_through_a_symlinked_path_still_exports(
    tmp_path: Path, signer: LocalSigner
) -> None:
    """Only links *inside* the capsule are refused; the path used to reach it may be one."""
    capsule = _capsule(tmp_path)
    link = tmp_path / "latest"
    link.symlink_to(capsule, target_is_directory=True)
    out = tmp_path / "out.zip"
    EvidenceBundleBuilder(link, signer, out).build()
    assert out.exists()


def test_regular_file_with_the_secret_bytes_is_unaffected(
    tmp_path: Path, signer: LocalSigner
) -> None:
    """The refusal is about links, not content: a real capture file is exported as is."""
    capsule = _capsule(tmp_path)
    (capsule / "outputs" / "result.txt").write_bytes(b"ordinary output\n")
    out = tmp_path / "out.zip"
    EvidenceBundleBuilder(capsule, signer, out).build()
    assert _zip_contains(out, b"ordinary output")
    assert not any(p.is_symlink() for p in capsule.rglob("*"))
    assert os.path.isdir(capsule / "inputs")


def test_read_regular_file_never_follows_a_link(tmp_path: Path, secret: Path) -> None:
    from novafabric.evidence.bundle import _read_regular_file

    link = tmp_path / "lineage.jsonl"
    link.symlink_to(secret)
    with pytest.raises(CapsuleSymlinkError):
        _read_regular_file(link)
    with pytest.raises(CapsuleValidationError, match="not a regular file"):
        _read_regular_file(tmp_path)
    with pytest.raises(FileNotFoundError):
        _read_regular_file(tmp_path / "absent.jsonl")
    plain = tmp_path / "plain.jsonl"
    plain.write_bytes(b"{}\n")
    assert _read_regular_file(plain) == b"{}\n"


def test_refusal_message_is_bounded(tmp_path: Path, signer: LocalSigner, secret: Path) -> None:
    capsule = _capsule(tmp_path)
    for i in range(25):
        (capsule / "inputs" / f"k{i:02d}").symlink_to(secret)
    err = _build_refused(capsule, signer, tmp_path / "out.zip")
    assert "holds 25 symlink(s)" in str(err)
    assert "and 5 more" in str(err)
