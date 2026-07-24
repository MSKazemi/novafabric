"""Collision policy (D5) and hostile-archive hardening (D3 / ADR-0009)."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from import_blob.helpers import (
    crafted_tar,
    export_capsules,
    make_capsule,
    write_signed_export,
)
from novafabric.evidence.signing import LocalSigner
from novafabric.import_blob.models import CollisionDetail
from novafabric.import_blob.service import import_batch
from novafabric.import_blob.unpack import UnpackError, safe_extract_tar


def _import(export_dir, store_root, receipts_dir, audit_path, registry_db, **kw):
    return import_batch(
        export_dir,
        capsule_root=store_root,
        receipts_dir=receipts_dir,
        audit_log_path=audit_path,
        db_path=registry_db,
        **kw,
    )


class TestCollisions:
    def test_collision_never_overwrites_and_reports_both_hashes(
        self,
        source_root: Path,
        store_root: Path,
        tmp_path: Path,
        signer: LocalSigner,
        public_pem: bytes,
        receipts_dir: Path,
        audit_path: Path,
        registry_db: Path,
    ) -> None:
        capsules = [
            make_capsule(source_root, "run-a", content="A"),
            make_capsule(source_root, "run-b", content="B"),
            make_capsule(source_root, "run-c", content="C"),
        ]
        export_dir = export_capsules(capsules, tmp_path / "export", signer)
        common = {
            "public_key_pem": public_pem,
        }
        first = _import(
            export_dir, store_root, receipts_dir, audit_path, registry_db, **common
        )
        assert first.exit_code == 0

        # Diverge run-a and run-b locally (legitimate local append is still a
        # collision, by design); delete run-c so it becomes a fresh import.
        (store_root / "run-a" / "outputs" / "stdout.txt").write_text("LOCAL-A")
        (store_root / "run-b" / "scores.jsonl").write_text('{"score": 1}\n')
        import shutil

        shutil.rmtree(store_root / "run-c")

        outcome = _import(
            export_dir, store_root, receipts_dir, audit_path, registry_db, **common
        )
        # Collisions take precedence in the exit code, after full processing.
        assert outcome.exit_code == 5
        actions = {m.capsule_id: m.action for m in outcome.receipt.members}
        assert actions == {
            "run-a": "collision",
            "run-b": "collision",
            "run-c": "imported",  # non-colliding members still import
        }
        assert outcome.receipt.counts.collisions == 2
        assert outcome.receipt.counts.imported == 1

        # Local capsule wins — bytes untouched.
        assert (store_root / "run-a" / "outputs" / "stdout.txt").read_text() == "LOCAL-A"
        assert (store_root / "run-b" / "scores.jsonl").is_file()
        # Both hashes in the report.
        detail = next(
            m.detail for m in outcome.receipt.members if m.capsule_id == "run-a"
        )
        assert isinstance(detail, CollisionDetail)
        assert detail.local_hash.startswith("sha256:")
        assert detail.manifest_hash.startswith("sha256:")
        assert detail.local_hash != detail.manifest_hash


class TestHostileArchives:
    def _import_crafted(
        self,
        blobs: dict[str, bytes],
        tmp_path: Path,
        store_root: Path,
        signer: LocalSigner,
        public_pem: bytes,
        receipts_dir: Path,
        audit_path: Path,
        registry_db: Path,
    ):
        export_dir = write_signed_export(tmp_path / "hostile", blobs, signer)
        return _import(
            export_dir, store_root, receipts_dir, audit_path, registry_db,
            public_key_pem=public_pem,
        )

    def test_path_traversal_member_rejected(
        self, tmp_path, store_root, signer, public_pem, receipts_dir,
        audit_path, registry_db,
    ) -> None:
        evil = crafted_tar(
            [
                ("capsule.yaml", b"run_id: evil\n"),
                ("../../escape.txt", b"pwned"),
            ]
        )
        outcome = self._import_crafted(
            {"evil": evil}, tmp_path, store_root, signer, public_pem,
            receipts_dir, audit_path, registry_db,
        )
        assert outcome.exit_code == 6
        member = outcome.receipt.members[0]
        assert member.action == "failed"
        assert "unsafe member name" in str(member.detail)
        # Nothing written: not in the store, not outside it.
        assert not (store_root / "evil").exists()
        assert not (store_root / ".import-staging").exists()
        assert not (store_root.parent / "escape.txt").exists()
        assert not (tmp_path / "escape.txt").exists()

    def test_absolute_path_member_rejected(
        self, tmp_path, store_root, signer, public_pem, receipts_dir,
        audit_path, registry_db,
    ) -> None:
        target = tmp_path / "abs-escape.txt"
        evil = crafted_tar([(str(target), b"pwned")])
        outcome = self._import_crafted(
            {"evil": evil}, tmp_path, store_root, signer, public_pem,
            receipts_dir, audit_path, registry_db,
        )
        assert outcome.exit_code == 6
        assert outcome.receipt.members[0].action == "failed"
        assert not target.exists()
        assert not (store_root / "evil").exists()

    def test_symlink_member_rejected(
        self, tmp_path, store_root, signer, public_pem, receipts_dir,
        audit_path, registry_db,
    ) -> None:
        link = tarfile.TarInfo(name="outputs/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        evil = crafted_tar([("capsule.yaml", b"run_id: evil\n"), link])
        outcome = self._import_crafted(
            {"evil": evil}, tmp_path, store_root, signer, public_pem,
            receipts_dir, audit_path, registry_db,
        )
        assert outcome.exit_code == 6
        assert outcome.receipt.members[0].action == "failed"
        assert "unsupported member type" in str(outcome.receipt.members[0].detail)
        assert not (store_root / "evil").exists()

    def test_hostile_member_does_not_block_good_members(
        self, source_root, tmp_path, store_root, signer, public_pem,
        receipts_dir, audit_path, registry_db,
    ) -> None:
        from novafabric.export_blob.packing import pack_capsule

        good = make_capsule(source_root, "run-good", content="ok")
        evil = crafted_tar([("../escape.txt", b"pwned")])
        outcome = self._import_crafted(
            {"run-good": pack_capsule(good), "evil": evil},
            tmp_path, store_root, signer, public_pem,
            receipts_dir, audit_path, registry_db,
        )
        assert outcome.exit_code == 6
        actions = {m.capsule_id: m.action for m in outcome.receipt.members}
        assert actions == {"run-good": "imported", "evil": "failed"}
        assert (store_root / "run-good" / "outputs" / "stdout.txt").read_text() == "ok"

    def test_hostile_archive_as_source_is_invalid(
        self, tmp_path, store_root, public_pem, receipts_dir, audit_path,
        registry_db,
    ) -> None:
        # A courier archive that tries to traverse on extraction refuses whole.
        archive = tmp_path / "hostile.tar"
        archive.write_bytes(crafted_tar([("../escape.txt", b"pwned")]))
        outcome = _import(
            archive, store_root, receipts_dir, audit_path, registry_db,
            public_key_pem=public_pem,
        )
        assert outcome.exit_code == 3
        assert outcome.receipt.verification.status == "INVALID"
        assert not (tmp_path.parent / "escape.txt").exists()


class TestSafeExtractUnit:
    def test_rejects_dotdot(self, tmp_path: Path) -> None:
        with pytest.raises(UnpackError, match="'\\.\\.' component"):
            safe_extract_tar(crafted_tar([("a/../../x", b"d")]), tmp_path / "out")

    def test_rejects_absolute(self, tmp_path: Path) -> None:
        with pytest.raises(UnpackError, match="absolute"):
            safe_extract_tar(crafted_tar([("/etc/x", b"d")]), tmp_path / "out")

    def test_rejects_windows_style_components(self, tmp_path: Path) -> None:
        with pytest.raises(UnpackError, match="suspicious"):
            safe_extract_tar(crafted_tar([("c:\\evil", b"d")]), tmp_path / "out")

    def test_rejects_hardlink(self, tmp_path: Path) -> None:
        info = tarfile.TarInfo(name="hard")
        info.type = tarfile.LNKTYPE
        info.linkname = "capsule.yaml"
        with pytest.raises(UnpackError, match="unsupported member type"):
            safe_extract_tar(crafted_tar([info]), tmp_path / "out")

    def test_rejects_garbage_bytes(self, tmp_path: Path) -> None:
        with pytest.raises(UnpackError, match="not a readable tar"):
            safe_extract_tar(b"definitely not a tar", tmp_path / "out")

    def test_extracts_files_and_dirs(self, tmp_path: Path) -> None:
        dir_info = tarfile.TarInfo(name="outputs")
        dir_info.type = tarfile.DIRTYPE
        data = crafted_tar([dir_info, ("outputs/x.txt", b"hello")])
        count = safe_extract_tar(data, tmp_path / "out")
        assert count == 1
        assert (tmp_path / "out" / "outputs" / "x.txt").read_bytes() == b"hello"
