"""Round-trip, idempotency, dry-run, and reindex behavior (ADR-0207 P1)."""

from __future__ import annotations

import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from import_blob.helpers import export_capsules, make_capsule, read_manifest
from novafabric.evidence.signing import LocalSigner
from novafabric.export_blob.packing import pack_capsule
from novafabric.import_blob.service import import_batch
from novafabric.object_capsule_store.cas import compute_sha256


def _import(
    export_dir: Path,
    store_root: Path,
    *,
    public_pem: bytes | None,
    receipts_dir: Path,
    audit_path: Path,
    registry_db: Path,
    **kwargs: object,
):
    return import_batch(
        export_dir,
        capsule_root=store_root,
        public_key_pem=public_pem,
        receipts_dir=receipts_dir,
        audit_log_path=audit_path,
        db_path=registry_db,
        **kwargs,  # type: ignore[arg-type]
    )


class TestRoundTrip:
    def test_export_import_reproduces_capsules_and_reindexes(
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
            make_capsule(source_root, "run-a", content="A", with_lineage=True),
            make_capsule(source_root, "run-b", content="B"),
            make_capsule(source_root, "run-c", content="C", parent_run_id="run-a"),
        ]
        export_dir = export_capsules(capsules, tmp_path / "export", signer)
        manifest = read_manifest(export_dir)

        outcome = _import(
            export_dir,
            store_root,
            public_pem=public_pem,
            receipts_dir=receipts_dir,
            audit_path=audit_path,
            registry_db=registry_db,
        )

        assert outcome.exit_code == 0
        receipt = outcome.receipt
        assert receipt.verification.mode == "signed"
        assert receipt.verification.status == "VALID"
        assert receipt.export_id == manifest["export_id"]
        assert receipt.batch_digest == manifest["batch_digest"]
        assert receipt.counts.imported == 3
        assert receipt.counts.skipped_existing == 0
        assert receipt.counts.collisions == 0
        assert receipt.counts.failed == 0

        # Byte-identical: the re-pack CAS address equals the manifest's.
        hashes_by_id = {m["capsule_id"]: m["content_hash"] for m in manifest["members"]}
        for run_id in ("run-a", "run-b", "run-c"):
            target = store_root / run_id
            assert target.is_dir()
            repacked = "sha256:" + compute_sha256(pack_capsule(target))
            assert repacked == hashes_by_id[run_id]

        # Lineage repopulated (run-a wrote a real lineage.jsonl).
        conn = sqlite3.connect(registry_db)
        edge_count = conn.execute("SELECT COUNT(*) FROM lineage_edges").fetchone()[0]
        assert edge_count >= 1
        rows = conn.execute("SELECT run_id FROM runs_cache").fetchall()
        assert {r[0] for r in rows} == {"run-a", "run-b", "run-c"}
        conn.close()
        assert receipt.reindex.lineage_capsules >= 1
        assert receipt.reindex.runs_cache_rows == 3
        assert receipt.reindex.errors == []

        # Receipt persisted; audit entry appended.
        assert outcome.receipt_path is not None and outcome.receipt_path.is_file()
        on_disk = json.loads(outcome.receipt_path.read_text())
        assert on_disk["import_id"] == receipt.import_id
        assert on_disk["counts"] == {
            "imported": 3,
            "skipped_existing": 0,
            "collisions": 0,
            "failed": 0,
        }
        audit_lines = [
            json.loads(line)
            for line in audit_path.read_text().splitlines()
            if line.strip()
        ]
        assert len(audit_lines) == 1
        assert audit_lines[0]["event_type"] == "capsule.import"
        assert audit_lines[0]["resource_id"] == receipt.import_id
        assert audit_lines[0]["details"]["verification_mode"] == "signed"

        # No staging residue.
        assert not (store_root / ".import-staging").exists()

    def test_second_import_is_idempotent_no_op(
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
            make_capsule(source_root, "run-a", content="A", with_lineage=True),
            make_capsule(source_root, "run-b", content="B"),
        ]
        export_dir = export_capsules(capsules, tmp_path / "export", signer)
        common = {
            "public_pem": public_pem,
            "receipts_dir": receipts_dir,
            "audit_path": audit_path,
            "registry_db": registry_db,
        }
        first = _import(export_dir, store_root, **common)
        assert first.exit_code == 0

        second = _import(export_dir, store_root, **common)
        assert second.exit_code == 0
        assert second.receipt.counts.imported == 0
        assert second.receipt.counts.skipped_existing == 2
        assert all(
            m.action == "skipped_existing" for m in second.receipt.members
        )

        # Re-running MUST NOT duplicate lineage edges or runs-cache rows.
        conn = sqlite3.connect(registry_db)
        assert conn.execute("SELECT COUNT(*) FROM runs_cache").fetchone()[0] == 2
        edge_count = conn.execute("SELECT COUNT(*) FROM lineage_edges").fetchone()[0]
        conn.close()
        assert edge_count == 1

    def test_empty_batch_is_valid_and_imports_nothing(
        self,
        store_root: Path,
        tmp_path: Path,
        signer: LocalSigner,
        public_pem: bytes,
        receipts_dir: Path,
        audit_path: Path,
        registry_db: Path,
    ) -> None:
        export_dir = export_capsules([], tmp_path / "export", signer)
        outcome = _import(
            export_dir,
            store_root,
            public_pem=public_pem,
            receipts_dir=receipts_dir,
            audit_path=audit_path,
            registry_db=registry_db,
        )
        assert outcome.exit_code == 0
        assert outcome.receipt.members == []
        assert outcome.receipt.counts.imported == 0
        assert outcome.receipt.verification.status == "VALID"
        assert list(store_root.iterdir()) == []

    @pytest.mark.parametrize("suffix", [".tar", ".tar.gz"])
    def test_archive_source(
        self,
        suffix: str,
        source_root: Path,
        store_root: Path,
        tmp_path: Path,
        signer: LocalSigner,
        public_pem: bytes,
        receipts_dir: Path,
        audit_path: Path,
        registry_db: Path,
    ) -> None:
        capsules = [make_capsule(source_root, "run-a", content="A")]
        export_dir = export_capsules(capsules, tmp_path / "export", signer)

        archive = tmp_path / f"batch{suffix}"
        mode = "w:gz" if suffix.endswith(".gz") else "w"
        with tarfile.open(archive, mode) as tf:
            for path in sorted(export_dir.rglob("*")):
                tf.add(path, arcname=str(path.relative_to(export_dir)))

        outcome = _import(
            archive,
            store_root,
            public_pem=public_pem,
            receipts_dir=receipts_dir,
            audit_path=audit_path,
            registry_db=registry_db,
        )
        assert outcome.exit_code == 0
        assert outcome.receipt.counts.imported == 1
        assert (store_root / "run-a" / "outputs" / "stdout.txt").read_text() == "A"

    def test_archive_source_with_wrapping_top_dir(
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
        capsules = [make_capsule(source_root, "run-a")]
        export_dir = export_capsules(capsules, tmp_path / "export", signer)
        archive = tmp_path / "batch.tar"
        with tarfile.open(archive, "w") as tf:
            tf.add(export_dir, arcname="my-export")
        outcome = _import(
            archive,
            store_root,
            public_pem=public_pem,
            receipts_dir=receipts_dir,
            audit_path=audit_path,
            registry_db=registry_db,
        )
        assert outcome.exit_code == 0
        assert outcome.receipt.counts.imported == 1


class TestDryRun:
    def test_dry_run_writes_nothing_and_matches_wet_classification(
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
        ]
        export_dir = export_capsules(capsules, tmp_path / "export", signer)
        common = {
            "public_pem": public_pem,
            "receipts_dir": receipts_dir,
            "audit_path": audit_path,
            "registry_db": registry_db,
        }

        dry = _import(export_dir, store_root, dry_run=True, **common)
        assert dry.exit_code == 0
        assert dry.receipt.dry_run is True
        # Zero writes to the store or indexes.
        assert list(store_root.iterdir()) == []
        assert not registry_db.exists()
        assert dry.receipt.reindex.lineage_capsules == 0
        assert dry.receipt.reindex.runs_cache_rows == 0
        # The receipt itself IS written (a drill leaves evidence too).
        assert dry.receipt_path is not None and dry.receipt_path.is_file()

        wet = _import(export_dir, store_root, **common)
        dry_actions = [(m.capsule_id, m.action) for m in dry.receipt.members]
        wet_actions = [(m.capsule_id, m.action) for m in wet.receipt.members]
        assert dry_actions == wet_actions

    def test_dry_run_classifies_skips_and_collisions(
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
        ]
        export_dir = export_capsules(capsules, tmp_path / "export", signer)
        common = {
            "public_pem": public_pem,
            "receipts_dir": receipts_dir,
            "audit_path": audit_path,
            "registry_db": registry_db,
        }
        _import(export_dir, store_root, **common)
        # Diverge run-b locally, then dry-run again.
        (store_root / "run-b" / "outputs" / "stdout.txt").write_text("MODIFIED")

        dry = _import(export_dir, store_root, dry_run=True, **common)
        assert dry.exit_code == 5
        actions = {m.capsule_id: m.action for m in dry.receipt.members}
        assert actions == {"run-a": "skipped_existing", "run-b": "collision"}
        # Still nothing rewritten.
        assert (store_root / "run-b" / "outputs" / "stdout.txt").read_text() == "MODIFIED"


class TestReindexControls:
    def test_no_reindex_unpacks_but_skips_indexes(
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
        capsules = [make_capsule(source_root, "run-a", with_lineage=True)]
        export_dir = export_capsules(capsules, tmp_path / "export", signer)
        outcome = _import(
            export_dir,
            store_root,
            public_pem=public_pem,
            receipts_dir=receipts_dir,
            audit_path=audit_path,
            registry_db=registry_db,
            reindex=False,
        )
        assert outcome.exit_code == 0
        assert (store_root / "run-a").is_dir()
        assert outcome.receipt.reindex.lineage_capsules == 0
        assert outcome.receipt.reindex.runs_cache_rows == 0
        assert not registry_db.exists()

    def test_orphan_parent_is_noted_in_receipt(
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
            make_capsule(source_root, "run-child", parent_run_id="run-gone"),
        ]
        export_dir = export_capsules(capsules, tmp_path / "export", signer)
        outcome = _import(
            export_dir,
            store_root,
            public_pem=public_pem,
            receipts_dir=receipts_dir,
            audit_path=audit_path,
            registry_db=registry_db,
        )
        assert outcome.exit_code == 0
        member = outcome.receipt.members[0]
        assert member.action == "imported"
        assert isinstance(member.detail, str)
        assert "orphan parent_run_id: run-gone" in member.detail

    def test_parent_in_batch_is_not_flagged_orphan(
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
            make_capsule(source_root, "run-parent"),
            make_capsule(source_root, "run-child", parent_run_id="run-parent"),
        ]
        export_dir = export_capsules(capsules, tmp_path / "export", signer)
        outcome = _import(
            export_dir,
            store_root,
            public_pem=public_pem,
            receipts_dir=receipts_dir,
            audit_path=audit_path,
            registry_db=registry_db,
        )
        assert outcome.exit_code == 0
        details = {m.capsule_id: m.detail for m in outcome.receipt.members}
        assert details["run-child"] is None
        assert (store_root / "run-parent").is_dir()
        assert (store_root / "run-child").is_dir()
