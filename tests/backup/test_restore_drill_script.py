"""scripts/backup_restore_drill.py — the helpers the CI backup/restore drill relies on.

The drill itself runs in its own CI workflow (``backup-restore-drill.yml``) against the
real ``nova`` CLI. These tests pin the pieces whose silent failure would make the drill
pass vacuously: the key-material filter, the byte-level comparison, the capsule
discovery, the tamper helper and the fail-closed exit path.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "backup_restore_drill.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("backup_restore_drill", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


drill = _load()


def _write_set(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def test_env_is_built_from_an_allowlist(tmp_path: Path) -> None:
    layout = drill.Layout(tmp_path)
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/real-user",
        "NOVAFABRIC_CAPSULE_DIR": "/real/capsules",
        "NOVAFABRIC_AUDIT_LOG_PATH": "/real/audit.jsonl",
        "OPENAI_API_KEY": "sk-not-for-the-drill",
    }
    env = drill.build_env(parent, layout)
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == str(layout.host)
    assert env["NOVAFABRIC_HOME"] == str(layout.home)
    assert env["NOVAFABRIC_AUDIT_LOG_PATH"] == str(layout.audit_log)
    assert "NOVAFABRIC_CAPSULE_DIR" not in env
    assert "OPENAI_API_KEY" not in env
    # Everything the drill deletes in the wipe step lives under host/; the vault does not.
    assert layout.home.is_relative_to(layout.host)
    assert layout.audit_log.is_relative_to(layout.host)
    assert not layout.vault.is_relative_to(layout.host)


def test_a_default_manifest_has_no_key_material() -> None:
    manifest = {
        "members": [
            {"path": "registry.db", "kind": "registry"},
            {"path": "capsules/RUN/manifest.json", "kind": "capsule"},
            {"path": "capsules/RUN/.seal/manifest.dsse", "kind": "capsule"},
            {"path": "config.redacted.yaml", "kind": "config"},
        ]
    }
    assert drill.key_material_members(manifest) == []


@pytest.mark.parametrize(
    "member",
    [
        {"path": "external/keyring/k.bin", "kind": "key_material"},
        {"path": "keys/novaseal/signing.key.pem", "kind": "other"},
        {"path": "seal/ca.crt.pem", "kind": "other"},
        {"path": "tls/server.key", "kind": "other"},
        {"path": "external/seal-config/novaseal.yaml", "kind": "config"},
    ],
)
def test_key_material_is_flagged(member: dict[str, str]) -> None:
    assert drill.key_material_members({"members": [member]}) == [member["path"]]


def test_capsule_member_paths_cover_home_and_external_stores_only_for_this_run() -> None:
    manifest = {
        "members": [
            {"path": "capsules/RUN/a.json"},
            {"path": "external/capsule-dir/RUN/b.json"},
            {"path": "capsules/OTHER/a.json"},
            {"path": "capsules/RUNX/a.json"},
            {"path": "registry.db"},
        ]
    }
    assert drill.capsule_member_paths(manifest, "RUN") == [
        "capsules/RUN/a.json",
        "external/capsule-dir/RUN/b.json",
    ]


def test_hash_tree_and_diff_detect_every_kind_of_change(tmp_path: Path) -> None:
    capsule = tmp_path / "c"
    (capsule / ".seal").mkdir(parents=True)
    (capsule / "manifest.json").write_bytes(b"{}")
    (capsule / ".seal" / "manifest.dsse").write_bytes(b"sig")
    before = drill.hash_tree(capsule)
    assert set(before) == {"manifest.json", ".seal/manifest.dsse"}
    assert drill.diff_hashes(before, drill.hash_tree(capsule)) == []

    (capsule / "manifest.json").write_bytes(b"{ }")
    (capsule / ".seal" / "manifest.dsse").unlink()
    (capsule / "extra.txt").write_bytes(b"x")
    problems = drill.diff_hashes(before, drill.hash_tree(capsule))
    assert problems == [
        "missing: .seal/manifest.dsse",
        "unexpected: extra.txt",
        "changed: manifest.json",
    ]


def test_hash_tree_refuses_a_symbolic_link(tmp_path: Path) -> None:
    (tmp_path / "real").write_bytes(b"x")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(drill.DrillError, match="symbolic link"):
        drill.hash_tree(tmp_path)


def test_find_single_capsule(tmp_path: Path) -> None:
    store = tmp_path / "capsules"
    with pytest.raises(drill.DrillError, match="no capsule store"):
        drill.find_single_capsule(store)
    store.mkdir()
    (store / ".index").mkdir()  # hidden bookkeeping is not a capsule
    (store / "stray-file").write_text("x")
    with pytest.raises(drill.DrillError, match="found 0"):
        drill.find_single_capsule(store)
    (store / "RUN1").mkdir()
    assert drill.find_single_capsule(store) == store / "RUN1"
    (store / "RUN2").mkdir()
    with pytest.raises(drill.DrillError, match="found 2"):
        drill.find_single_capsule(store)


def test_tamper_flips_exactly_one_byte_of_one_member(tmp_path: Path) -> None:
    src, dst = tmp_path / "set.tar.gz", tmp_path / "tampered.tar.gz"
    members = {"manifest.json": b'{"members": []}', "capsules/RUN/trace.jsonl": b"abc"}
    _write_set(src, members)
    drill.tamper_set(src, dst, "capsules/RUN/trace.jsonl")
    with tarfile.open(dst, "r:gz") as tar:
        got = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}  # type: ignore[union-attr]
    assert got["manifest.json"] == members["manifest.json"]
    assert got["capsules/RUN/trace.jsonl"] == b"abb"


def test_tamper_refuses_a_missing_or_empty_member(tmp_path: Path) -> None:
    src = tmp_path / "set.tar.gz"
    _write_set(src, {"manifest.json": b"{}", "capsules/RUN/empty": b""})
    for name in ("capsules/RUN/absent", "capsules/RUN/empty"):
        with pytest.raises(drill.DrillError, match="not found or empty"):
            drill.tamper_set(src, tmp_path / "out.tar.gz", name)


def test_read_manifest_requires_a_members_list(tmp_path: Path) -> None:
    good, bad = tmp_path / "good.tar.gz", tmp_path / "bad.tar.gz"
    _write_set(good, {"manifest.json": json.dumps({"members": [{"path": "x"}]}).encode()})
    _write_set(bad, {"manifest.json": b'{"set_id": "s"}'})
    assert drill.read_manifest(good)["members"] == [{"path": "x"}]
    with pytest.raises(drill.DrillError, match="no members list"):
        drill.read_manifest(bad)


def test_a_non_empty_work_dir_is_refused(tmp_path: Path) -> None:
    (tmp_path / "leftover").write_text("x")
    assert drill.main(["--work-dir", str(tmp_path)]) == 2


def test_a_failing_nova_fails_the_drill_and_names_the_step(tmp_path: Path) -> None:
    fake = tmp_path / "bin" / "nova"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\necho 'boom' >&2\nexit 3\n")
    fake.chmod(0o755)
    report = tmp_path / "report.json"
    work = tmp_path / "work"
    assert drill.main(["--nova", str(fake), "--work-dir", str(work), "--report", str(report)]) == 1
    data = json.loads(report.read_text())
    assert data["ok"] is False
    assert data["failed_step"] == "version-backup"
    assert data["commands"][0]["exit_code"] == 3
    assert "boom" in (work / "logs" / data["commands"][0]["log"]).read_text()
