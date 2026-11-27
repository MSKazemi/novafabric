"""Empty capsule directories survive backup -> restore (manifest 0.2.2 ``empty_dirs``).

A backup set used to hold regular-file members only, so an empty directory was lost:
a capsule's empty ``inputs/`` did not come back, and the restored capsule failed
``nova validate`` ("missing directory: inputs") while ``nova restore`` reported success.
Found by the CI backup/restore drill (``scripts/backup_restore_drill.py``).
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from novafabric.backup import RestoreError, create_backup, restore_backup
from novafabric.backup.coverage import copy_tree_files
from novafabric.backup.models import BackupManifest
from novafabric.backup.verify import read_manifest, verify_backup


@pytest.fixture()
def _no_signing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("novafabric.backup.create.load_signing_profile", lambda: None)


def _registry(home: Path) -> None:
    from novafabric.registry.store import get_connection, init_schema

    home.mkdir(parents=True, exist_ok=True)
    conn = get_connection(home / "registry.db")
    init_schema(conn)
    conn.close()


def _home_with_empty_dirs(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    _registry(home)
    capsule = home / "capsules" / "run-001"
    (capsule / "inputs").mkdir(parents=True)
    (capsule / "outputs" / "nested").mkdir(parents=True)
    (capsule / "manifest.json").write_text('{"run_id": "run-001"}')
    (capsule / "logs").mkdir()
    (capsule / "logs" / "stdout.txt").write_text("hello\n")
    return home


def _rewrite_manifest(archive: Path, dest: Path, **changes: object) -> Path:
    """Copy *archive* with ``manifest.json`` fields replaced (unsigned sets only)."""
    with tarfile.open(archive, "r:gz") as src, tarfile.open(dest, "w:gz") as out:
        for info in src.getmembers():
            fh = src.extractfile(info)
            data = fh.read() if fh is not None else b""
            if info.name == "manifest.json":
                body = json.loads(data)
                body.update(changes)
                data = json.dumps(body).encode()
                info.size = len(data)
            out.addfile(info, io.BytesIO(data))
    return dest


def test_copy_tree_files_reports_directories_without_files(tmp_path: Path) -> None:
    src = _home_with_empty_dirs(tmp_path) / "capsules"
    empty: list[str] = []
    copied = copy_tree_files(src, tmp_path / "stage", empty_dirs=empty)
    assert sorted(copied) == ["run-001/logs/stdout.txt", "run-001/manifest.json"]
    assert empty == ["run-001/inputs", "run-001/outputs", "run-001/outputs/nested"]


def test_empty_dirs_round_trip(tmp_path: Path, _no_signing: None) -> None:
    home = _home_with_empty_dirs(tmp_path)
    archive = create_backup(tmp_path / "set.tar.gz", home=home).archive_path

    manifest = read_manifest(archive)
    assert manifest.manifest_schema_version == "0.2.2"
    assert manifest.empty_dirs == [
        "capsules/run-001/inputs",
        "capsules/run-001/outputs",
        "capsules/run-001/outputs/nested",
    ]
    assert verify_backup(archive).ok

    target = tmp_path / "restored"
    result = restore_backup(archive, home=target)
    assert result.ok is True
    capsule = target / "capsules" / "run-001"
    assert (capsule / "inputs").is_dir()
    assert list((capsule / "inputs").iterdir()) == []
    assert (capsule / "outputs" / "nested").is_dir()
    assert (capsule / "logs" / "stdout.txt").read_text() == "hello\n"
    extract = next(s for s in result.steps if s.name == "extract")
    assert "3 empty directories recreated" in extract.detail


def test_a_set_without_empty_dirs_keeps_the_previous_manifest_body(
    tmp_path: Path, _no_signing: None
) -> None:
    home = tmp_path / "home"
    _registry(home)
    (home / "capsules" / "run-001").mkdir(parents=True)
    (home / "capsules" / "run-001" / "manifest.json").write_text("{}")
    archive = create_backup(tmp_path / "set.tar.gz", home=home).archive_path
    with tarfile.open(archive, "r:gz") as tar:
        fh = tar.extractfile("manifest.json")
        assert fh is not None
        body = json.loads(fh.read())
    # Not even `"empty_dirs": null`: a 0.2.1 reader (closed schema) still parses it.
    assert "empty_dirs" not in body
    assert read_manifest(archive).empty_dirs is None


def test_signable_bytes_omit_an_absent_empty_dirs() -> None:
    """Sets signed before 0.2.2 have no ``empty_dirs`` key; their signed bytes must not change."""
    manifest = BackupManifest(
        set_id="01HZZZZZZZZZZZZZZZZZZZZZZZ",
        created_at="2026-07-16T00:00:00+00:00",
        nova_version="0.0.0-test",
        members=[],
    )
    assert b"empty_dirs" not in manifest.signable_bytes()
    with_dirs = manifest.model_copy(update={"empty_dirs": ["capsules/r/inputs"]})
    assert b'"empty_dirs":["capsules/r/inputs"]' in with_dirs.signable_bytes()


@pytest.mark.parametrize(
    "entry",
    [
        "../outside",
        "capsules/../../outside",
        "/abs/capsules/x",
        "registry-dir",  # not under a capsule tree
        "capsules/run-001/keys",  # key-material deny filter
    ],
)
def test_an_unsafe_empty_dir_entry_refuses_the_restore(
    tmp_path: Path, _no_signing: None, entry: str
) -> None:
    home = _home_with_empty_dirs(tmp_path)
    archive = create_backup(tmp_path / "set.tar.gz", home=home).archive_path
    crafted = _rewrite_manifest(archive, tmp_path / "crafted.tar.gz", empty_dirs=[entry])
    target = tmp_path / "restored"
    with pytest.raises(RestoreError):
        restore_backup(crafted, home=target)
    assert not (tmp_path / "outside").exists()
    assert not (target / "capsules").exists()
