"""State, evidence and secret files are replaced durably: fsync the file, rename, fsync the directory.

Line-by-line review X2: the legal-hold ledger rewrite on release, captured media
blobs and several other writers renamed an un-fsynced temporary file into place
and never fsynced the directory. On XFS or Lustre a crash right after can leave a
zero-length or missing file — for ``holds.jsonl`` that reads as *no holds*, so
deletion fails open. Each case below drives one writer through its public (or
module-level) entry point and asserts the order with :mod:`_durability`.
``tests/test_rename_sites_are_durable.py`` keeps new rename sites honest.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from _durability import DurabilityRecorder


@pytest.fixture
def durability(monkeypatch: pytest.MonkeyPatch) -> DurabilityRecorder:
    return DurabilityRecorder(monkeypatch)


@pytest.fixture
def umask_077() -> Iterator[None]:
    old = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(old)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


# --- retention: the legal-hold ledger ---------------------------------------


def test_hold_release_rewrites_the_ledger_durably_and_keeps_its_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durability: DurabilityRecorder
) -> None:
    from novafabric.retention import holds

    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    record = holds.place_hold("legal", "litigation 42", actor="test")
    os.chmod(record.source, 0o600)
    durability.clear()

    assert holds.release_hold(record.hold_id, actor="test") == ["legal"]

    durability.assert_durable(record.source)
    assert _mode(record.source) == 0o600
    (line,) = record.source.read_text(encoding="utf-8").splitlines()
    assert json.loads(line)["released_at"] is not None


# --- serve: the issued-token store (secret) ----------------------------------


def test_token_store_rewrite_is_durable_private_and_never_follows_a_planted_temp_link(
    tmp_path: Path, durability: DurabilityRecorder
) -> None:
    from novafabric.serve import token_store

    path = tmp_path / "tokens.jsonl"
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    (tmp_path / "tokens.tmp").symlink_to(victim)  # the old fixed temp name

    token_store._write_all(path, [{"fingerprint": "abc"}])

    durability.assert_durable(path)
    assert _mode(path) == 0o600
    assert victim.read_text() == "untouched"
    assert json.loads(path.read_text()) == {"fingerprint": "abc"}


# --- capture: content-addressed media blobs ----------------------------------


def test_media_blob_is_stored_durably_at_the_umask_mode(
    tmp_path: Path, durability: DurabilityRecorder, umask_077: None
) -> None:
    from novafabric.capture.media import _store_blob

    capsule = tmp_path / "capsule"
    capsule.mkdir()
    hex_digest = "ab" * 32

    rel = _store_blob(capsule, hex_digest, b"\x89PNG", "image/png")

    assert rel is not None
    durability.assert_durable(capsule / rel)
    assert (capsule / rel).read_bytes() == b"\x89PNG"
    assert _mode(capsule / rel) == 0o600  # 0o666 & ~umask: never wider than before


def test_media_blob_refuses_a_linked_outputs_directory(tmp_path: Path) -> None:
    from novafabric.capture.media import _store_blob

    capsule = tmp_path / "capsule"
    capsule.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (capsule / "outputs").symlink_to(elsewhere)

    assert _store_blob(capsule, "cd" * 32, b"x", "image/png") is None  # degrades, never writes through
    assert list(elsewhere.iterdir()) == []


# --- capsule files rewritten in place ----------------------------------------


def test_replace_capsule_file_is_durable(tmp_path: Path, durability: DurabilityRecorder) -> None:
    from novafabric._capsule_fs import replace_capsule_file

    replace_capsule_file(tmp_path, "capsule.yaml", b"run_id: r1\n")

    durability.assert_durable(tmp_path / "capsule.yaml")


# --- sessions, dashboards, exports, snapshots --------------------------------


def test_session_manifest_save_is_durable(tmp_path: Path, durability: DurabilityRecorder) -> None:
    from novafabric.session.manifest import new_session, save_session

    path = save_session(new_session(), root=tmp_path / "sessions")

    durability.assert_durable(path)


def test_dashboard_document_write_is_durable(
    tmp_path: Path, durability: DurabilityRecorder
) -> None:
    from novafabric.dashboards._store import _write_if_changed

    path, changed = _write_if_changed(tmp_path / "d" / "w.json", "{}\n")

    assert changed
    durability.assert_durable(path)


def test_local_export_destination_replaces_a_worm_manifest_durably(
    tmp_path: Path, durability: DurabilityRecorder
) -> None:
    from novafabric.export_blob.destinations import LocalDirDestination

    dest = LocalDirDestination(tmp_path / "export")
    dest.put_manifest("manifest.json", b"{}", worm=True)
    dest.put_manifest("manifest.json", b'{"v": 2}', worm=True)  # replaces a 0o444 file

    durability.assert_durable(tmp_path / "export" / "manifest.json")
    assert (tmp_path / "export" / "manifest.json").read_bytes() == b'{"v": 2}'
    assert _mode(tmp_path / "export" / "manifest.json") == 0o444


def test_topology_snapshot_save_is_durable(
    tmp_path: Path, durability: DurabilityRecorder
) -> None:
    from novafabric.serve.topology.snapshot_store_3d import SnapshotStore3D

    store = SnapshotStore3D(tmp_path / "snapshots")
    store.save("w1", {"nodes": []})

    durability.assert_durable(store.fine_dir / "w1.snap")
    assert store.get("w1") == {"nodes": []}
