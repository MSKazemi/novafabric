"""Edge and failure paths: unsigned tamper variants, reindex degradation."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from import_blob.helpers import (
    export_capsules,
    make_capsule,
    manifest_path,
    read_manifest,
)
from novafabric.evidence.signing import LocalSigner
from novafabric.export_blob.digest import compute_batch_digest
from novafabric.export_blob.models import ExportMember
from novafabric.import_blob.service import (
    _staged_parent_run_id,
    import_batch,
)
from novafabric.import_blob.unpack import UnpackError, _validate_member_name


def _import(export_dir, store_root, receipts_dir, audit_path, registry_db, **kw):
    return import_batch(
        export_dir,
        capsule_root=store_root,
        receipts_dir=receipts_dir,
        audit_log_path=audit_path,
        db_path=registry_db,
        **kw,
    )


@pytest.fixture()
def export_dir(source_root: Path, tmp_path: Path, signer: LocalSigner) -> Path:
    capsules = [
        make_capsule(source_root, "run-a", content="A"),
        make_capsule(source_root, "run-b", content="B"),
    ]
    return export_capsules(capsules, tmp_path / "export", signer)


class TestUnsignedTamperVariants:
    def test_count_lie_is_invalid_unsigned(
        self, export_dir, store_root, receipts_dir, audit_path, registry_db
    ) -> None:
        raw = read_manifest(export_dir)
        raw["count"] = raw["count"] + 1
        manifest_path(export_dir).write_text(json.dumps(raw))
        outcome = _import(
            export_dir, store_root, receipts_dir, audit_path, registry_db,
            allow_unsigned=True,
        )
        assert outcome.exit_code == 3
        assert any("count" in p for p in outcome.receipt.verification.problems)
        assert list(store_root.iterdir()) == []

    def test_size_lie_with_fixed_digest_is_incomplete_unsigned(
        self, export_dir, store_root, receipts_dir, audit_path, registry_db
    ) -> None:
        # An attacker who lies about size AND recomputes the digest gets past
        # the consistency check — the per-member size check still refuses.
        raw = read_manifest(export_dir)
        raw["members"][0]["size"] += 1
        raw["batch_digest"] = compute_batch_digest(
            [ExportMember.model_validate(m) for m in raw["members"]]
        )
        manifest_path(export_dir).write_text(json.dumps(raw))
        outcome = _import(
            export_dir, store_root, receipts_dir, audit_path, registry_db,
            allow_unsigned=True,
        )
        assert outcome.exit_code == 4
        assert any(
            "size mismatch" in p for p in outcome.receipt.verification.problems
        )
        assert list(store_root.iterdir()) == []


class TestReindexDegradation:
    def test_corrupt_lineage_jsonl_is_recorded_not_fatal(
        self, source_root, tmp_path, store_root, receipts_dir, audit_path,
        registry_db, signer, public_pem,
    ) -> None:
        capsule = make_capsule(source_root, "run-a")
        (capsule / "lineage.jsonl").write_text("{not json\n")
        export_dir = export_capsules([capsule], tmp_path / "export", signer)
        outcome = _import(
            export_dir, store_root, receipts_dir, audit_path, registry_db,
            public_key_pem=public_pem,
        )
        # The capsule still imports; the lineage failure is recorded.
        assert outcome.exit_code == 0
        assert (store_root / "run-a").is_dir()
        assert outcome.receipt.counts.imported == 1
        assert any("lineage run-a" in e for e in outcome.receipt.reindex.errors)
        assert outcome.receipt.reindex.lineage_capsules == 0

    def test_registry_connection_failure_is_recorded_not_fatal(
        self, export_dir, store_root, receipts_dir, audit_path, registry_db,
        public_pem, monkeypatch,
    ) -> None:
        def _boom(_db_path=None):
            raise sqlite3.OperationalError("registry unavailable")

        monkeypatch.setattr("novafabric.registry.store.get_connection", _boom)
        outcome = _import(
            export_dir, store_root, receipts_dir, audit_path, registry_db,
            public_key_pem=public_pem,
        )
        # Capsules land; the derived-index failure is reported, not fatal.
        assert outcome.exit_code == 0
        assert (store_root / "run-a").is_dir()
        assert any(
            "runs_cache connection" in e for e in outcome.receipt.reindex.errors
        )
        assert outcome.receipt.reindex.runs_cache_rows == 0

    def test_blob_vanishing_after_verification_fails_member(
        self, export_dir, store_root, receipts_dir, audit_path, registry_db,
        public_pem, monkeypatch,
    ) -> None:
        # TOCTOU defense: bytes verified in step 5, gone by step 7.
        from novafabric.export_blob.destinations import LocalDirDestination

        real_get_blob = LocalDirDestination.get_blob
        calls = {"n": 0}

        def flaky_get_blob(self, content_hash):
            calls["n"] += 1
            if calls["n"] > 2:  # step 5 verifies both members first
                return None
            return real_get_blob(self, content_hash)

        monkeypatch.setattr(LocalDirDestination, "get_blob", flaky_get_blob)
        outcome = _import(
            export_dir, store_root, receipts_dir, audit_path, registry_db,
            public_key_pem=public_pem,
        )
        assert outcome.exit_code == 6
        failed = [m for m in outcome.receipt.members if m.action == "failed"]
        assert failed
        assert "changed or vanished" in str(failed[0].detail)


class TestClassificationFailure:
    def test_unpackable_existing_capsule_is_failed_member(
        self, export_dir, store_root, receipts_dir, audit_path, registry_db,
        public_pem, monkeypatch,
    ) -> None:
        # An existing local run-a that cannot be re-packed for comparison.
        from novafabric.export_blob.packing import CapsulePackError

        (store_root / "run-a").mkdir()
        (store_root / "run-a" / "capsule.yaml").write_text("run_id: run-a\n")

        def _boom(path):
            raise CapsulePackError(f"unreadable: {path}")

        monkeypatch.setattr("novafabric.import_blob.service.pack_capsule", _boom)
        outcome = _import(
            export_dir, store_root, receipts_dir, audit_path, registry_db,
            public_key_pem=public_pem,
        )
        assert outcome.exit_code == 6
        actions = {m.capsule_id: m.action for m in outcome.receipt.members}
        assert actions["run-a"] == "failed"
        assert actions["run-b"] == "imported"


class TestSmallHelpers:
    def test_staged_parent_run_id_missing_manifest(self, tmp_path: Path) -> None:
        assert _staged_parent_run_id(tmp_path) is None

    def test_staged_parent_run_id_non_dict_yaml(self, tmp_path: Path) -> None:
        (tmp_path / "capsule.yaml").write_text("- just\n- a list\n")
        assert _staged_parent_run_id(tmp_path) is None

    def test_validate_member_name_empty(self) -> None:
        with pytest.raises(UnpackError, match="empty"):
            _validate_member_name(".")
