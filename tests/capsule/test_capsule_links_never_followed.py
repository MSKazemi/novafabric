"""No capsule exporter or copier follows a symbolic link out of the capsule.

An adversarial review showed that after ``nova export-evidence`` learnt to refuse
links, four other exporters still copied a link's *target* into their output:
``export_blob.pack_capsule`` (``nova export-blob``), ``CapsuleMigrator.migrate``
(``nova migrate``, ``/api/capsule-migrate``), ``export_ro_crate``
(``nova export-rocrate``, ``/api/compliance/export/rocrate``) and
``backup.create_backup`` (``nova backup create``). A link such as
``inputs/data.txt -> ~/.ssh/id_rsa`` put the key into the archive.

They now share :mod:`novafabric._capsule_fs`. Each one refuses any link with a
named error that is also a :class:`CapsuleLinkError`, and writes nothing. Every
variant is covered: a link to an outside file, a dangling link, a directory
link, a link that stays inside the capsule, a symlinked parent directory, and a
link planted after the check, just before the copy.
"""

from __future__ import annotations

import io
import os
import sqlite3
import sys
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from novafabric import _capsule_fs
from novafabric._capsule_fs import (
    CapsuleLinkError,
    CapsuleSpecialFileError,
    copy_capsule_tree,
    list_capsule,
    open_capsule_file,
    read_capsule_file,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")

CANARY = b"CANARY-SSH-PRIVATE-KEY-7f3a"
RUN_ID = "01J9ZZTESTCAPSULELINKS0001"


@pytest.fixture()
def secret(tmp_path: Path) -> Path:
    path = tmp_path / "outside" / ".ssh" / "id_rsa"
    path.parent.mkdir(parents=True)
    path.write_bytes(CANARY + b"\n")
    return path


def _write_capsule(cap: Path) -> Path:
    (cap / "inputs").mkdir(parents=True)
    (cap / "outputs").mkdir()
    (cap / "capsule.yaml").write_text(
        yaml.safe_dump({"run_id": RUN_ID, "schema_version": "0.1.0", "status": "success"})
    )
    (cap / "trace.jsonl").write_text('{"span": 1}\n')
    (cap / "inputs" / "data.txt").write_text("input\n")
    (cap / "outputs" / "result.txt").write_text("output\n")
    return cap


@pytest.fixture()
def capsule(tmp_path: Path) -> Path:
    return _write_capsule(tmp_path / "caps" / RUN_ID)


# -- the variants -------------------------------------------------------------


def _file_link(cap: Path, secret: Path) -> None:
    os.symlink(secret, cap / "inputs" / "stolen")


def _top_level_link(cap: Path, secret: Path) -> None:
    os.symlink(secret, cap / "notes.txt")


def _dangling_link(cap: Path, secret: Path) -> None:
    os.symlink(secret.parent / "does-not-exist", cap / "inputs" / "gone")


def _dir_link(cap: Path, secret: Path) -> None:
    os.symlink(secret.parent, cap / "inputs" / "ssh")


def _inside_link(cap: Path, secret: Path) -> None:
    os.symlink(cap / "trace.jsonl", cap / "outputs" / "trace-copy.jsonl")


def _parent_dir_link(cap: Path, secret: Path) -> None:
    """``outputs/`` itself is replaced by a link to the secret's directory."""
    for child in (cap / "outputs").iterdir():
        child.unlink()
    (cap / "outputs").rmdir()
    os.symlink(secret.parent, cap / "outputs")


VARIANTS: dict[str, Callable[[Path, Path], None]] = {
    "file-link": _file_link,
    "top-level-link": _top_level_link,
    "dangling": _dangling_link,
    "dir-link": _dir_link,
    "inside-link": _inside_link,
    "parent-dir-link": _parent_dir_link,
}


# -- the exporters ------------------------------------------------------------
# Each returns every byte it wrote, so a test can assert the canary is absent.


def _all_zip_bytes(path: Path) -> bytes:
    with zipfile.ZipFile(path) as zf:
        return b"".join(zf.read(n) for n in zf.namelist())


def _run_pack(cap: Path, work: Path) -> bytes:
    from novafabric.export_blob.packing import CapsulePackError, pack_capsule

    try:
        return pack_capsule(cap)
    except CapsuleLinkError as exc:
        assert isinstance(exc, CapsulePackError), "callers catch CapsulePackError"
        raise


def _run_migrate(cap: Path, work: Path) -> bytes:
    from novafabric.capsule_migration._converter import CapsuleMigrationError, CapsuleMigrator

    out = work / "migrated"
    try:
        CapsuleMigrator().migrate(cap, out)
    except CapsuleLinkError as exc:
        assert isinstance(exc, CapsuleMigrationError), "callers catch CapsuleMigrationError"
        assert not out.exists(), "a refused migration leaves no partial output"
        raise
    return b"".join(p.read_bytes() for p in out.rglob("*") if p.is_file())


def _run_rocrate(cap: Path, work: Path) -> bytes:
    from novafabric.compliance.export.ro_crate import export_ro_crate

    out = work / "crate.zip"
    try:
        export_ro_crate(cap, out)
    except CapsuleLinkError:
        assert not out.exists(), "a refused export writes no archive"
        raise
    return _all_zip_bytes(out)


def _run_backup(cap: Path, work: Path) -> bytes:
    from novafabric.backup.create import BackupCreateError, create_backup

    home = cap.parent.parent / "home"
    (home / "capsules").mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(home / "registry.db") as db:
        db.execute("create table if not exists t(x)")
    os.replace(cap, home / "capsules" / cap.name)
    out = work / "set.tar.gz"
    try:
        create_backup(out, home=home, audit_log_path=work / "audit.jsonl")
    except CapsuleLinkError as exc:
        assert isinstance(exc, BackupCreateError), "the CLI catches BackupCreateError"
        assert not out.exists(), "a refused backup writes no set"
        raise
    with tarfile.open(out) as tf:
        return b"".join(
            tf.extractfile(m).read()  # type: ignore[union-attr]
            for m in tf.getmembers()
            if m.isfile()
        )


def _run_evidence(cap: Path, work: Path) -> bytes:
    from novafabric.evidence.bundle import EvidenceBundleBuilder
    from novafabric.evidence.signing import LocalSigner, generate_keypair

    priv, _ = generate_keypair(work / "keys")
    out = work / "bundle.zip"
    EvidenceBundleBuilder(
        cap, LocalSigner(priv), out, audit_log_path=work / "audit.jsonl"
    ).build()
    return _all_zip_bytes(out)


EXPORTERS: dict[str, Callable[[Path, Path], bytes]] = {
    "export-blob": _run_pack,
    "migrate": _run_migrate,
    "ro-crate": _run_rocrate,
    "backup": _run_backup,
}


@pytest.mark.parametrize("variant", sorted(VARIANTS))
@pytest.mark.parametrize("exporter", sorted(EXPORTERS))
def test_exporter_refuses_every_symlink_variant(
    tmp_path: Path, capsule: Path, secret: Path, exporter: str, variant: str
) -> None:
    VARIANTS[variant](capsule, secret)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(CapsuleLinkError) as info:
        EXPORTERS[exporter](capsule, work)
    assert info.value.links, "the refusal names the link"
    assert CANARY.decode() not in str(info.value)


@pytest.mark.parametrize("exporter", sorted(EXPORTERS))
def test_exporter_still_exports_a_clean_capsule(
    tmp_path: Path, capsule: Path, exporter: str
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    data = EXPORTERS[exporter](capsule, work)
    assert b"input\n" in data or exporter == "ro-crate"
    assert b"span" in data


@pytest.mark.parametrize("exporter", sorted(EXPORTERS))
def test_exporter_never_follows_a_link_planted_after_the_listing(
    tmp_path: Path,
    capsule: Path,
    secret: Path,
    exporter: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Swap a regular file for a link between the walk and the read."""
    real_list = _capsule_fs.list_capsule

    def racing_list(root: Path, **kwargs: object) -> _capsule_fs.CapsuleListing:
        listing = real_list(root, **kwargs)  # type: ignore[arg-type]
        # backup lists $HOME/capsules, so the file is one level down there
        rel = next(f for f in listing.files if f.endswith("trace.jsonl"))
        victim = listing.root / rel
        victim.unlink()
        os.symlink(secret, victim)
        return listing

    monkeypatch.setattr(_capsule_fs, "list_capsule", racing_list)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(CapsuleLinkError):
        EXPORTERS[exporter](capsule, work)


def _captured_capsule(tmp_path: Path) -> Path:
    from novafabric.capture.orchestrator import CaptureOrchestrator

    return (
        CaptureOrchestrator(base_dir=tmp_path / "runs")
        .run(command=[sys.executable, "-c", "pass"])
        .capsule_dir
    )


def test_evidence_export_refusal_is_a_capsule_link_error(tmp_path: Path, secret: Path) -> None:
    from novafabric.evidence.bundle import CapsuleSymlinkError, CapsuleValidationError

    capsule = _captured_capsule(tmp_path)
    _file_link(capsule, secret)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(CapsuleSymlinkError) as info:
        _run_evidence(capsule, work)
    assert isinstance(info.value, CapsuleValidationError)
    assert isinstance(info.value, CapsuleLinkError)


def test_evidence_export_never_follows_a_link_planted_after_validation(
    tmp_path: Path, secret: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The staging copy used ``shutil.copytree``, which re-resolves each path."""
    from novafabric.evidence import bundle as bundle_mod

    capsule = _captured_capsule(tmp_path)
    real_list = _capsule_fs.list_capsule
    calls = {"n": 0}

    def racing_list(root: Path, **kwargs: object) -> _capsule_fs.CapsuleListing:
        listing = real_list(root, **kwargs)  # type: ignore[arg-type]
        calls["n"] += 1
        victim = listing.root / "trace.jsonl"
        if not victim.is_symlink():
            victim.unlink()
            os.symlink(secret, victim)
        return listing

    monkeypatch.setattr(_capsule_fs, "list_capsule", racing_list)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(bundle_mod.CapsuleSymlinkError):
        _run_evidence(capsule, work)
    assert not (work / "bundle.zip").exists()


# -- the helper itself --------------------------------------------------------


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_list_capsule_names_every_link(capsule: Path, secret: Path, variant: str) -> None:
    VARIANTS[variant](capsule, secret)
    with pytest.raises(CapsuleLinkError) as info:
        list_capsule(capsule, operation="a test")
    assert len(info.value.links) == 1
    where = info.value.links[0]
    expected = {
        "dangling": "dangling",
        "inside-link": "points inside the capsule",
    }.get(variant, "points outside the capsule")
    assert expected in where


def test_list_capsule_accepts_a_root_reached_through_a_link(
    tmp_path: Path, capsule: Path
) -> None:
    alias = tmp_path / "latest"
    os.symlink(capsule, alias)
    listing = list_capsule(alias, operation="a test")
    assert "capsule.yaml" in listing.files
    assert listing.dirs == ("inputs", "outputs")


def test_list_capsule_skips_special_files(capsule: Path) -> None:
    os.mkfifo(capsule / "inputs" / "pipe")
    listing = list_capsule(capsule, operation="a test")
    assert listing.skipped == ("inputs/pipe",)
    assert "inputs/pipe" not in listing.files


@pytest.mark.parametrize("variant", ["file-link", "parent-dir-link", "dir-link"])
def test_open_capsule_file_refuses_a_link_on_any_component(
    capsule: Path, secret: Path, variant: str
) -> None:
    VARIANTS[variant](capsule, secret)
    rel = {
        "file-link": "inputs/stolen",
        "parent-dir-link": "outputs/id_rsa",
        "dir-link": "inputs/ssh/id_rsa",
    }[variant]
    with pytest.raises(CapsuleLinkError):
        read_capsule_file(capsule, rel)


def test_open_capsule_file_refuses_a_fifo_without_blocking(capsule: Path) -> None:
    os.mkfifo(capsule / "inputs" / "pipe")
    with pytest.raises(CapsuleSpecialFileError):
        open_capsule_file(capsule, "inputs/pipe")


@pytest.mark.parametrize("bad", ["../x", "/etc/passwd", "", "inputs/../../x"])
def test_open_capsule_file_refuses_paths_that_leave_the_capsule(
    capsule: Path, bad: str
) -> None:
    with pytest.raises(ValueError):
        open_capsule_file(capsule, bad)


def test_open_capsule_file_missing_is_file_not_found(capsule: Path) -> None:
    with pytest.raises(FileNotFoundError):
        open_capsule_file(capsule, "inputs/nope")


def test_read_capsule_file_reads_and_bounds(capsule: Path) -> None:
    assert read_capsule_file(capsule, "inputs/data.txt") == b"input\n"
    assert read_capsule_file(capsule, "inputs/data.txt", limit=2) == b"in"


def test_copy_capsule_tree_keeps_bytes_modes_and_mtimes(tmp_path: Path, capsule: Path) -> None:
    src = capsule / "inputs" / "data.txt"
    os.chmod(src, 0o600)
    os.utime(src, ns=(1_000_000_000, 2_000_000_000))
    dest = tmp_path / "copy"
    copy_capsule_tree(capsule, dest, operation="a test")
    copied = dest / "inputs" / "data.txt"
    assert copied.read_bytes() == b"input\n"
    assert copied.stat().st_mode & 0o777 == 0o600
    assert copied.stat().st_mtime_ns == 2_000_000_000


def test_copy_capsule_tree_removes_a_partial_copy_on_refusal(
    tmp_path: Path, capsule: Path, secret: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_list = _capsule_fs.list_capsule

    def racing_list(root: Path, **kwargs: object) -> _capsule_fs.CapsuleListing:
        listing = real_list(root, **kwargs)  # type: ignore[arg-type]
        (listing.root / "trace.jsonl").unlink()
        os.symlink(secret, listing.root / "trace.jsonl")
        return listing

    monkeypatch.setattr(_capsule_fs, "list_capsule", racing_list)
    dest = tmp_path / "copy"
    with pytest.raises(CapsuleLinkError):
        copy_capsule_tree(capsule, dest, operation="a test")
    assert not dest.exists()


def test_copy_capsule_tree_copies_a_hard_link_as_a_regular_file(
    tmp_path: Path, capsule: Path
) -> None:
    """Hard links are a documented limit (see the module docstring), not refused."""
    os.link(capsule / "inputs" / "data.txt", capsule / "inputs" / "data-again.txt")
    dest = tmp_path / "copy"
    copy_capsule_tree(capsule, dest, operation="a test")
    assert (dest / "inputs" / "data-again.txt").read_bytes() == b"input\n"
    assert (dest / "inputs" / "data-again.txt").stat().st_nlink == 1


def test_pack_capsule_is_still_deterministic(capsule: Path) -> None:
    from novafabric.export_blob.packing import pack_capsule

    first = pack_capsule(capsule)
    assert first == pack_capsule(capsule)
    with tarfile.open(fileobj=io.BytesIO(first)) as tf:
        names = tf.getnames()
    assert names == sorted(names)
    assert "inputs/data.txt" in names


# -- further capsule readers found by the sweep ---------------------------------


def test_client_upload_zip_refuses_a_link(capsule: Path, secret: Path) -> None:
    from novafabric.client._client import _zip_directory

    _file_link(capsule, secret)
    with pytest.raises(CapsuleLinkError):
        _zip_directory(capsule)


def test_client_upload_zip_still_packs_a_clean_capsule(capsule: Path) -> None:
    from novafabric.client._client import _zip_directory

    with zipfile.ZipFile(io.BytesIO(_zip_directory(capsule))) as zf:
        assert f"{capsule.name}/inputs/data.txt" in zf.namelist()


@pytest.mark.parametrize("variant", ["top-level-link", "file-link"])
def test_examiner_bagit_refuses_a_link(
    tmp_path: Path, capsule: Path, secret: Path, variant: str
) -> None:
    from novafabric.compliance.export.examiner import BagItExporter

    if variant == "top-level-link":
        (capsule / "capsule.yaml").unlink()
        os.symlink(secret, capsule / "capsule.yaml")
    else:
        _file_link(capsule, secret)
    out = tmp_path / "bags"
    with pytest.raises(CapsuleLinkError):
        BagItExporter(capsule_id=RUN_ID, data_dir=capsule).export(output_dir=out)
    assert not (out / f"{RUN_ID}-bag.zip").exists()


def test_audit_bundle_refuses_a_linked_capsule(
    tmp_path: Path, capsule: Path, secret: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from novafabric.cli import audit as audit_cli

    data_dir = tmp_path / "data"
    (data_dir / "capsules").mkdir(parents=True)
    os.replace(capsule, data_dir / "capsules" / RUN_ID)
    _file_link(data_dir / "capsules" / RUN_ID, secret)
    report = SimpleNamespace(
        coverages=[SimpleNamespace(capsule_ids=[RUN_ID])],
        model_dump_json=lambda **_: "{}",
        overall_score=1.0,
        covered_controls=1,
        total_controls=1,
    )
    monkeypatch.setattr(
        audit_cli,
        "_load_engine",
        lambda *_: SimpleNamespace(scan=lambda: report, capsules_dir=data_dir / "capsules"),
    )
    out = tmp_path / "bundle.zip"
    result = CliRunner().invoke(
        audit_cli.app, ["bundle", "--data-dir", str(data_dir), "--output", str(out)]
    )
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert not out.exists()
    assert CANARY.decode() not in result.output


def test_migrate_schema_refuses_a_linked_manifest(capsule: Path, tmp_path: Path) -> None:
    from novafabric.cli.migrate_schema import migrate_capsule

    victim = tmp_path / "outside" / "victim.json"
    victim.parent.mkdir(exist_ok=True)
    victim.write_text('{"schema_version": "0.1.0"}')
    os.symlink(victim, capsule / "manifest.json")
    result = migrate_capsule(capsule, dry_run=False, backup=True)
    assert result.failed
    assert victim.read_text() == '{"schema_version": "0.1.0"}'
    assert not (capsule / "manifest.json.v0.bak").exists()


@pytest.mark.parametrize("dangling", [True, False])
def test_aibom_never_writes_through_a_link(tmp_path: Path, dangling: bool) -> None:
    from novafabric.compliance.export.aibom import AIBOMExporter

    cap = _write_capsule(tmp_path / "cap")
    victim = tmp_path / "outside" / "victim.txt"
    victim.parent.mkdir(exist_ok=True)
    if not dangling:
        victim.write_text("untouched\n")
    os.symlink(victim, cap / "aibom.json")
    exporter = AIBOMExporter()
    with pytest.raises(CapsuleLinkError):
        exporter.export_json(exporter.build_aibom(cap), cap / "aibom.json")
    if dangling:
        assert not victim.exists()
    else:
        assert victim.read_text() == "untouched\n"


def test_migrate_never_reads_a_linked_manifest(tmp_path: Path, capsule: Path) -> None:
    """Validation read capsule.yaml before the copy refused, echoing the target's value."""
    from novafabric.capsule_migration._converter import (
        CapsuleMigrationLinkError,
        CapsuleMigrator,
    )

    victim = tmp_path / "outside" / "kubeconfig.yaml"
    victim.parent.mkdir(exist_ok=True)
    victim.write_text("schema_version: CANARY-TOKEN-VALUE\n")
    (capsule / "capsule.yaml").unlink()
    os.symlink(victim, capsule / "capsule.yaml")
    with pytest.raises(CapsuleMigrationLinkError) as info:
        CapsuleMigrator().migrate(capsule, tmp_path / "out")
    assert "CANARY-TOKEN-VALUE" not in str(info.value)
    assert info.value.as_dict()["rule"] == "a capsule holds no symbolic links"
