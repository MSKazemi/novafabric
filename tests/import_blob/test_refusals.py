"""Fail-closed verification: refusals import nothing, exit codes per spec."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from import_blob.helpers import (
    blob_path,
    export_capsules,
    make_capsule,
    manifest_path,
    read_manifest,
)
from novafabric.evidence.signing import LocalSigner, generate_keypair
from novafabric.import_blob.service import ImportUsageError, import_batch


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
def export_dir(
    source_root: Path, tmp_path: Path, signer: LocalSigner
) -> Path:
    capsules = [
        make_capsule(source_root, "run-a", content="A"),
        make_capsule(source_root, "run-b", content="B"),
    ]
    return export_capsules(capsules, tmp_path / "export", signer)


class TestUsageErrors:
    def test_no_key_and_no_allow_unsigned_refused(
        self, export_dir, store_root, receipts_dir, audit_path, registry_db
    ) -> None:
        with pytest.raises(ImportUsageError, match="--allow-unsigned"):
            _import(export_dir, store_root, receipts_dir, audit_path, registry_db)

    def test_key_and_allow_unsigned_together_refused(
        self, export_dir, store_root, receipts_dir, audit_path, registry_db,
        public_pem,
    ) -> None:
        with pytest.raises(ImportUsageError, match="mutually exclusive"):
            _import(
                export_dir, store_root, receipts_dir, audit_path, registry_db,
                public_key_pem=public_pem, allow_unsigned=True,
            )

    def test_missing_source_refused(
        self, tmp_path, store_root, receipts_dir, audit_path, registry_db,
        public_pem,
    ) -> None:
        with pytest.raises(ImportUsageError, match="does not exist"):
            _import(
                tmp_path / "nope", store_root, receipts_dir, audit_path,
                registry_db, public_key_pem=public_pem,
            )

    def test_non_archive_file_source_refused(
        self, tmp_path, store_root, receipts_dir, audit_path, registry_db,
        public_pem,
    ) -> None:
        bogus = tmp_path / "manifest.txt"
        bogus.write_text("not an archive")
        with pytest.raises(ImportUsageError, match=".tar"):
            _import(
                bogus, store_root, receipts_dir, audit_path, registry_db,
                public_key_pem=public_pem,
            )

    def test_source_inside_store_refused(
        self, store_root, receipts_dir, audit_path, registry_db, public_pem,
        signer, tmp_path,
    ) -> None:
        inner_capsules = tmp_path / "caps"
        inner_capsules.mkdir()
        capsules = [make_capsule(inner_capsules, "run-a")]
        inside = export_capsules(capsules, store_root / "an-export", signer)
        with pytest.raises(ImportUsageError, match="overlaps the capsule store"):
            _import(
                inside, store_root, receipts_dir, audit_path, registry_db,
                public_key_pem=public_pem,
            )


class TestInvalidRefusals:
    def test_missing_manifest_is_invalid_exit_3(
        self, tmp_path, store_root, receipts_dir, audit_path, registry_db,
        public_pem,
    ) -> None:
        empty = tmp_path / "empty-export"
        empty.mkdir()
        outcome = _import(
            empty, store_root, receipts_dir, audit_path, registry_db,
            public_key_pem=public_pem,
        )
        assert outcome.exit_code == 3
        assert outcome.receipt.verification.status == "INVALID"
        assert list(store_root.iterdir()) == []
        # Refusal still leaves a receipt + audit entry.
        assert outcome.receipt_path is not None and outcome.receipt_path.is_file()
        audit = [
            json.loads(line) for line in audit_path.read_text().splitlines() if line
        ]
        assert audit[0]["event_type"] == "capsule.import"
        assert audit[0]["details"]["verification_status"] == "INVALID"

    def test_manifest_schema_violation_exit_3(
        self, export_dir, store_root, receipts_dir, audit_path, registry_db,
        public_pem,
    ) -> None:
        raw = read_manifest(export_dir)
        del raw["count"]
        manifest_path(export_dir).write_text(json.dumps(raw))
        outcome = _import(
            export_dir, store_root, receipts_dir, audit_path, registry_db,
            public_key_pem=public_pem,
        )
        assert outcome.exit_code == 3
        assert outcome.receipt.verification.status == "INVALID"
        assert any("count" in p for p in outcome.receipt.verification.problems)
        assert list(store_root.iterdir()) == []

    def test_tampered_manifest_member_list_exit_3(
        self, export_dir, store_root, receipts_dir, audit_path, registry_db,
        public_pem,
    ) -> None:
        raw = read_manifest(export_dir)
        raw["members"] = raw["members"][:1]  # drop a member the signer attested
        raw["count"] = 1
        manifest_path(export_dir).write_text(json.dumps(raw))
        outcome = _import(
            export_dir, store_root, receipts_dir, audit_path, registry_db,
            public_key_pem=public_pem,
        )
        assert outcome.exit_code == 3
        assert outcome.receipt.verification.status == "INVALID"
        assert list(store_root.iterdir()) == []
        # Every manifest member is recorded as not_processed.
        assert all(m.action == "not_processed" for m in outcome.receipt.members)

    def test_bad_signature_refused_without_allow_unsigned(
        self, export_dir, store_root, receipts_dir, audit_path, registry_db,
        tmp_path,
    ) -> None:
        _priv, other_pub = generate_keypair(tmp_path / "other-keys")
        outcome = _import(
            export_dir, store_root, receipts_dir, audit_path, registry_db,
            public_key_pem=other_pub.read_bytes(),
        )
        assert outcome.exit_code == 3
        assert outcome.receipt.verification.status == "INVALID"
        assert any("signature" in p.lower() for p in outcome.receipt.verification.problems)
        assert list(store_root.iterdir()) == []

    def test_bad_signature_proceeds_with_allow_unsigned(
        self, export_dir, store_root, receipts_dir, audit_path, registry_db,
    ) -> None:
        # No key at all — authorship unverified, content checks still enforced.
        outcome = _import(
            export_dir, store_root, receipts_dir, audit_path, registry_db,
            allow_unsigned=True,
        )
        assert outcome.exit_code == 0
        assert outcome.receipt.verification.mode == "unsigned"
        assert outcome.receipt.verification.status == "VALID"
        assert outcome.receipt.counts.imported == 2
        # Permanently recorded in receipt + audit.
        on_disk = json.loads(outcome.receipt_path.read_text())
        assert on_disk["verification"]["mode"] == "unsigned"
        audit = [
            json.loads(line) for line in audit_path.read_text().splitlines() if line
        ]
        assert audit[0]["details"]["verification_mode"] == "unsigned"

    def test_digest_tamper_refused_even_with_allow_unsigned(
        self, export_dir, store_root, receipts_dir, audit_path, registry_db,
    ) -> None:
        raw = read_manifest(export_dir)
        raw["members"] = raw["members"][:1]
        raw["count"] = 1  # leave batch_digest stale → recompute mismatch
        manifest_path(export_dir).write_text(json.dumps(raw))
        outcome = _import(
            export_dir, store_root, receipts_dir, audit_path, registry_db,
            allow_unsigned=True,
        )
        assert outcome.exit_code == 3
        assert outcome.receipt.verification.status == "INVALID"
        assert any("batch_digest" in p for p in outcome.receipt.verification.problems)
        assert list(store_root.iterdir()) == []


class TestIncompleteRefusals:
    @pytest.mark.parametrize("mode", ["signed", "unsigned"])
    def test_flipped_bit_in_blob_refuses_exit_4(
        self, mode, export_dir, store_root, receipts_dir, audit_path,
        registry_db, public_pem,
    ) -> None:
        raw = read_manifest(export_dir)
        target = blob_path(export_dir, raw["members"][0]["content_hash"])
        data = bytearray(target.read_bytes())
        data[len(data) // 2] ^= 0xFF
        target.write_bytes(bytes(data))

        kwargs = (
            {"public_key_pem": public_pem}
            if mode == "signed"
            else {"allow_unsigned": True}
        )
        outcome = _import(
            export_dir, store_root, receipts_dir, audit_path, registry_db,
            **kwargs,
        )
        assert outcome.exit_code == 4
        assert outcome.receipt.verification.status == "INCOMPLETE"
        assert list(store_root.iterdir()) == []
        assert all(m.action == "not_processed" for m in outcome.receipt.members)
        assert outcome.receipt.counts.imported == 0

    @pytest.mark.parametrize("mode", ["signed", "unsigned"])
    def test_missing_blob_refuses_exit_4(
        self, mode, export_dir, store_root, receipts_dir, audit_path,
        registry_db, public_pem,
    ) -> None:
        raw = read_manifest(export_dir)
        blob_path(export_dir, raw["members"][0]["content_hash"]).unlink()
        kwargs = (
            {"public_key_pem": public_pem}
            if mode == "signed"
            else {"allow_unsigned": True}
        )
        outcome = _import(
            export_dir, store_root, receipts_dir, audit_path, registry_db,
            **kwargs,
        )
        assert outcome.exit_code == 4
        assert outcome.receipt.verification.status == "INCOMPLETE"
        assert any("missing" in p for p in outcome.receipt.verification.problems)
        assert list(store_root.iterdir()) == []

    def test_size_mismatch_refuses_exit_4_unsigned(
        self, export_dir, store_root, receipts_dir, audit_path, registry_db,
    ) -> None:
        # A size lie with a matching hash is impossible; lie about size instead.
        raw = read_manifest(export_dir)
        raw["members"][0]["size"] += 1
        # keep count/digest fixups out — digest covers size, so this is caught
        # at the digest stage in unsigned mode, still refusing.
        manifest_path(export_dir).write_text(json.dumps(raw))
        outcome = _import(
            export_dir, store_root, receipts_dir, audit_path, registry_db,
            allow_unsigned=True,
        )
        assert outcome.exit_code == 3  # digest mismatch (INVALID) — fail-closed
        assert list(store_root.iterdir()) == []
