"""Seal-aware atomic ``capsule.yaml`` rewrite (``novafabric.capsule._manifest_write``).

Covers the helper directly and the three commands that use it —
``nova consent record``, ``nova insurance sla verify --write`` and
``nova science receipt build --write`` — against a *real* NovaSeal seal, so
"refused" is checked by ``nova verify`` still passing afterwards.
"""

from __future__ import annotations

import datetime
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.capsule import _manifest_write as mw
from novafabric.cli.main import app

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parents[2]
SLA_TERMS = REPO_ROOT / "tests" / "fixtures" / "risk-transfer" / "sla-terms-valid.json"
MANIFEST = {"run_id": "mw-001", "status": "success"}
ENV_DIGEST = "sha256:" + "a" * 64


def _flat(text: str) -> str:
    return " ".join(text.split())


# ── helper unit tests ─────────────────────────────────────────────────────


def _plain(tmp_path: Path, mode: int = 0o640) -> Path:
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: old\n", encoding="utf-8")
    os.chmod(cap / "capsule.yaml", mode)
    return cap


def test_unsealed_write_replaces_and_preserves_mode(tmp_path: Path) -> None:
    cap = _plain(tmp_path, 0o640)
    result = mw.write_capsule_manifest(cap, "run_id: new\n")
    assert result == mw.ManifestWriteResult(path=cap / "capsule.yaml", was_sealed=False)
    assert (cap / "capsule.yaml").read_text() == "run_id: new\n"
    assert stat.S_IMODE((cap / "capsule.yaml").stat().st_mode) == 0o640
    assert not list(cap.glob(".capsule.yaml.*"))


def test_missing_manifest_is_created_with_default_mode(tmp_path: Path) -> None:
    cap = tmp_path / "cap"
    cap.mkdir()
    mw.write_capsule_manifest(cap, "run_id: x\n")
    assert stat.S_IMODE((cap / "capsule.yaml").stat().st_mode) == 0o644


def test_sealed_refused_and_forced(tmp_path: Path) -> None:
    cap = _plain(tmp_path)
    (cap / ".seal").mkdir()
    before = (cap / "capsule.yaml").read_bytes()
    with pytest.raises(mw.SealedCapsuleError, match="--force-unseal"):
        mw.write_capsule_manifest(cap, "run_id: new\n")
    assert (cap / "capsule.yaml").read_bytes() == before
    result = mw.write_capsule_manifest(cap, "run_id: new\n", force_unseal=True)
    assert result.was_sealed is True
    assert (cap / "capsule.yaml").read_text() == "run_id: new\n"
    assert "--force-unseal" in mw.UNSEAL_WARNING


def test_dangling_seal_symlink_counts_as_sealed(tmp_path: Path) -> None:
    cap = _plain(tmp_path)
    (cap / ".seal").symlink_to(tmp_path / "gone")
    assert mw.is_sealed(cap)
    with pytest.raises(mw.SealedCapsuleError):
        mw.write_capsule_manifest(cap, "x: 1\n")


def test_symlinked_manifest_refused_target_untouched(tmp_path: Path) -> None:
    cap = tmp_path / "cap"
    cap.mkdir()
    victim = tmp_path / "novaseal.yaml"
    victim.write_text("profile: local\n", encoding="utf-8")
    (cap / "capsule.yaml").symlink_to(victim)
    with pytest.raises(mw.UnsafeManifestPathError, match="symlink"):
        mw.write_capsule_manifest(cap, "pwned: true\n", force_unseal=True)
    assert victim.read_text() == "profile: local\n"
    assert (cap / "capsule.yaml").is_symlink()


def test_symlinked_capsule_dir_refused(tmp_path: Path) -> None:
    real = _plain(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(mw.UnsafeManifestPathError, match="directory is a symlink"):
        mw.write_capsule_manifest(link, "x: 1\n")
    assert (real / "capsule.yaml").read_text() == "run_id: old\n"


@pytest.mark.parametrize("kind", ["not-dir", "missing", "manifest-dir"])
def test_non_regular_paths_refused(tmp_path: Path, kind: str) -> None:
    cap = tmp_path / "cap"
    if kind == "not-dir":
        cap.write_text("x")
    elif kind == "manifest-dir":
        (cap / "capsule.yaml").mkdir(parents=True)
    with pytest.raises(mw.UnsafeManifestPathError):
        mw.write_capsule_manifest(cap, "x: 1\n")


def test_manifest_lstat_error_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _plain(tmp_path)
    real_lstat = os.lstat

    def lstat(path: Any, *a: Any, **kw: Any) -> os.stat_result:
        if str(path).endswith("capsule.yaml"):
            raise PermissionError("denied")
        return real_lstat(path, *a, **kw)

    monkeypatch.setattr(mw.os, "lstat", lstat)
    with pytest.raises(mw.UnsafeManifestPathError, match="PermissionError"):
        mw.write_capsule_manifest(cap, "x: 1\n")


def test_replace_failure_leaves_original_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = _plain(tmp_path)

    def boom(*_: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(mw.ManifestWriteError, match="cannot write capsule.yaml"):
        mw.write_capsule_manifest(cap, "run_id: new\n")
    assert (cap / "capsule.yaml").read_text() == "run_id: old\n"
    assert not list(cap.glob(".capsule.yaml.*"))


def test_symlink_swapped_in_before_replace_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-replace re-check catches a manifest swapped for a symlink mid-write."""
    cap = _plain(tmp_path)
    victim = tmp_path / "victim.yaml"
    victim.write_text("keep\n")
    real_fsync = os.fsync

    def fsync_then_swap(fd: int) -> None:
        real_fsync(fd)
        (cap / "capsule.yaml").unlink()
        (cap / "capsule.yaml").symlink_to(victim)

    monkeypatch.setattr(mw.os, "fsync", fsync_then_swap)
    with pytest.raises(mw.UnsafeManifestPathError):
        mw.write_capsule_manifest(cap, "x: 1\n")
    assert victim.read_text() == "keep\n"
    assert not list(cap.glob(".capsule.yaml.*"))


# ── end-to-end: the three commands against a real NovaSeal seal ───────────


@pytest.fixture
def sealed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A capsule whose capsule.yaml equals the signed payload; ``nova verify`` passes."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    from novafabric.trust.novaseal import KeyConfig, NovaSeal

    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "seal.key"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "NovaSeal-MW")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "seal.crt"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    merkle_db = tmp_path / "merkle.db"
    config_path = tmp_path / "novaseal.yaml"
    config_path.write_text(
        f"profile: local\nkey_path: {key_path}\ncert_path: {cert_path}\n"
        f"tsa_url: \nmerkle_db: {merkle_db}\n"
    )
    # `nova verify --seal-config` exports this; register it so it is restored.
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(config_path))
    seal = NovaSeal(
        config=KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path)),
        tsa_url="",
        db_path=str(merkle_db),
    )
    cap = tmp_path / "sealed-cap"
    cap.mkdir()
    (cap / "capsule.yaml").write_text(yaml.safe_dump(MANIFEST, sort_keys=False))
    bundle = seal.seal(dict(MANIFEST))
    seal_dir = cap / ".seal"
    seal_dir.mkdir()
    (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal_dir / "manifest.dsse.tsr").write_bytes(bundle.tsr)
    (seal_dir / "log-entry.json").write_text(json.dumps(bundle.log_entry), encoding="utf-8")
    return cap, config_path


def _verify(cap: Path, config: Path) -> Any:
    return runner.invoke(app, ["verify", str(cap), "--seal-config", str(config)])


def _consent(cap: Path) -> list[str]:
    return [
        "consent", "record", "--capsule", str(cap), "--subject", "human:did:example:bob",
        "--purpose", "dpv:ServiceProvision", "--scope", "dpv:Store",
        "--given-at", "2026-07-16T00:00:00Z",
    ]  # fmt: skip


def _insurance(cap: Path) -> list[str]:
    return [
        "insurance", "sla", "verify", "--capsule", str(cap), "--sla", str(SLA_TERMS),
        "--observed", "99.2", "--write",
    ]  # fmt: skip


def _science(cap: Path) -> list[str]:
    return ["science", "receipt", "build", "--capsule", str(cap), "--env", ENV_DIGEST, "--write"]


COMMANDS: list[tuple[str, Callable[[Path], list[str]], int, str]] = [
    ("consent", _consent, 1, "consent"),
    ("insurance", _insurance, 2, "risk_transfer"),
    ("science", _science, 2, "science_provenance"),
]


@pytest.mark.parametrize(("label", "argv", "refuse_code", "facet"), COMMANDS)
def test_sealed_capsule_refused_and_still_verifies(
    sealed: tuple[Path, Path],
    label: str,
    argv: Callable[[Path], list[str]],
    refuse_code: int,
    facet: str,
) -> None:
    cap, config = sealed
    assert _verify(cap, config).exit_code == 0
    before = (cap / "capsule.yaml").read_bytes()
    result = runner.invoke(app, argv(cap))
    assert result.exit_code == refuse_code, result.output
    assert "--force-unseal" in _flat(result.output)
    assert "sealed" in result.output
    assert (cap / "capsule.yaml").read_bytes() == before
    assert not list(cap.glob(".capsule.yaml.*"))
    after = _verify(cap, config)
    assert after.exit_code == 0, after.output


@pytest.mark.parametrize(("label", "argv", "refuse_code", "facet"), COMMANDS)
def test_force_unseal_writes_and_warns(
    sealed: tuple[Path, Path],
    label: str,
    argv: Callable[[Path], list[str]],
    refuse_code: int,
    facet: str,
) -> None:
    cap, config = sealed
    result = runner.invoke(app, [*argv(cap), "--force-unseal"])
    assert result.exit_code == 0, result.output
    assert "no longer matches" in _flat(result.output)
    written = yaml.safe_load((cap / "capsule.yaml").read_text())
    if facet == "consent":
        assert written["facets"]["conversation"]["consent"]
    else:
        assert facet in written["facets"]
    # The honest consequence the warning names: the old seal no longer binds.
    assert _verify(cap, config).exit_code != 0


@pytest.mark.parametrize(("label", "argv", "refuse_code", "facet"), COMMANDS)
def test_symlinked_manifest_refused_by_every_command(
    tmp_path: Path,
    label: str,
    argv: Callable[[Path], list[str]],
    refuse_code: int,
    facet: str,
) -> None:
    cap = tmp_path / "cap"
    cap.mkdir()
    victim = tmp_path / "novaseal.yaml"
    victim.write_text(yaml.safe_dump(MANIFEST, sort_keys=False))
    before = victim.read_bytes()
    (cap / "capsule.yaml").symlink_to(victim)
    result = runner.invoke(app, argv(cap))
    assert result.exit_code == refuse_code, result.output
    assert "symlink" in result.output
    assert victim.read_bytes() == before


@pytest.mark.parametrize(("label", "argv", "refuse_code", "facet"), COMMANDS)
def test_replace_failure_keeps_original_for_every_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    argv: Callable[[Path], list[str]],
    refuse_code: int,
    facet: str,
) -> None:
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "capsule.yaml").write_text(yaml.safe_dump(MANIFEST, sort_keys=False))
    before = (cap / "capsule.yaml").read_bytes()

    def boom(*_: Any) -> None:
        raise OSError("crash mid-rename")

    monkeypatch.setattr(os, "replace", boom)
    result = runner.invoke(app, argv(cap))
    assert result.exit_code == refuse_code, result.output
    assert "cannot write capsule.yaml" in result.output
    assert (cap / "capsule.yaml").read_bytes() == before
    assert not list(cap.glob(".capsule.yaml.*"))


@pytest.mark.parametrize(("label", "argv", "refuse_code", "facet"), COMMANDS)
def test_unsealed_happy_path_unchanged(
    tmp_path: Path,
    label: str,
    argv: Callable[[Path], list[str]],
    refuse_code: int,
    facet: str,
) -> None:
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "capsule.yaml").write_text(yaml.safe_dump(MANIFEST, sort_keys=False))
    result = runner.invoke(app, argv(cap))
    assert result.exit_code == 0, result.output
    assert "no longer matches" not in _flat(result.output)
    written = yaml.safe_load((cap / "capsule.yaml").read_text())
    assert written["run_id"] == MANIFEST["run_id"]
    assert written["facets"]
