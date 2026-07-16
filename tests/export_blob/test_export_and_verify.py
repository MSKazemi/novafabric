"""End-to-end local export + offline verification (ADR-0141 D2–D4)."""

from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path

import jsonschema
import pytest

from export_blob.helpers import make_capsule
from novafabric.evidence.intoto import dsse_sign_payload
from novafabric.evidence.signing import LocalSigner
from novafabric.export_blob.destinations import LocalDirDestination, blob_key
from novafabric.export_blob.digest import canonical_signing_payload, compute_batch_digest
from novafabric.export_blob.models import (
    MANIFEST_FILENAME,
    MANIFEST_PAYLOAD_TYPE,
    ExportManifest,
    WormIntent,
)
from novafabric.export_blob.service import (
    CapsuleSelection,
    ExportSelectionError,
    VerifyStatus,
    export_batch,
    select_capsules,
    verify_export_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((REPO_ROOT / "schemas" / "export-manifest.schema.json").read_text())


def _export(
    capsule_dirs: list[Path],
    dest_dir: Path,
    signer: LocalSigner,
    *,
    query: str | None = None,
    worm: WormIntent | None = None,
):
    selection = CapsuleSelection(
        capsule_dirs=capsule_dirs,
        query=query,
        query_resolved_at="2026-07-14T00:00:00.000000Z",
    )
    dest = LocalDirDestination(dest_dir, uri=str(dest_dir))
    return export_batch(selection, dest, signer, worm=worm)


def _manifest_path(dest_dir: Path) -> Path:
    return dest_dir / MANIFEST_FILENAME


class TestLocalExport:
    def test_manifest_written_and_schema_valid(
        self, capsule_root: Path, tmp_path: Path, signer: LocalSigner
    ) -> None:
        capsules = [
            make_capsule(capsule_root, "run-a", content="A"),
            make_capsule(capsule_root, "run-b", content="B"),
        ]
        dest = tmp_path / "out"
        result = _export(capsules, dest, signer)

        raw = json.loads(_manifest_path(dest).read_text())
        validator = jsonschema.Draft202012Validator(
            SCHEMA, format_checker=jsonschema.FormatChecker()
        )
        assert list(validator.iter_errors(raw)) == []

        assert raw["count"] == 2 == len(raw["members"])
        assert {m["capsule_id"] for m in raw["members"]} == {"run-a", "run-b"}
        assert result.written == 2 and result.skipped == 0
        assert raw["producer"]["tool"] == "novafabric"
        assert raw["query"] is None  # explicit member list

    def test_blobs_are_content_addressed_at_dest(
        self, capsule_root: Path, tmp_path: Path, signer: LocalSigner
    ) -> None:
        capsule = make_capsule(capsule_root, "run-a")
        dest = tmp_path / "out"
        result = _export([capsule], dest, signer)
        member = result.manifest.members[0]
        blob = dest / blob_key(member.content_hash)
        assert blob.is_file()
        assert blob.stat().st_size == member.size

    def test_idempotent_rerun_skips_blobs_same_digest_new_export_id(
        self, capsule_root: Path, tmp_path: Path, signer: LocalSigner
    ) -> None:
        capsules = [make_capsule(capsule_root, "run-a")]
        dest = tmp_path / "out"
        first = _export(capsules, dest, signer)
        second = _export(capsules, dest, signer)
        assert second.written == 0 and second.skipped == 1
        assert second.manifest.batch_digest == first.manifest.batch_digest
        assert second.manifest.export_id != first.manifest.export_id

    def test_empty_batch_is_a_valid_signed_manifest(
        self, tmp_path: Path, signer: LocalSigner, public_pem: bytes
    ) -> None:
        dest = tmp_path / "out"
        result = _export([], dest, signer, query="created_at >= '2099-01-01'")
        assert result.manifest.count == 0
        report = verify_export_manifest(_manifest_path(dest), public_pem)
        assert report.status is VerifyStatus.VALID

    def test_duplicate_content_dedups_blob_but_lists_both_members(
        self, capsule_root: Path, tmp_path: Path, signer: LocalSigner
    ) -> None:
        c1 = make_capsule(capsule_root, "twin", with_run_id=False)
        nested = capsule_root / "nested"
        nested.mkdir()
        c2 = make_capsule(nested, "twin", with_run_id=False)
        # identical bytes, different capsule dirs → same CAS address
        dest = tmp_path / "out"
        result = _export([c1, c2], dest, signer)
        manifest = result.manifest
        assert manifest.count == 2
        assert len({m.content_hash for m in manifest.members}) == 1
        assert result.written == 1  # one deduped blob
        assert len(list((dest / "objects").iterdir())) == 1

    def test_interrupted_export_leaves_no_manifest_and_resumes(
        self, capsule_root: Path, tmp_path: Path, signer: LocalSigner, public_pem: bytes
    ) -> None:
        capsules = [
            make_capsule(capsule_root, "run-a", content="A"),
            make_capsule(capsule_root, "run-b", content="B"),
        ]
        dest_dir = tmp_path / "out"

        class FailingDest(LocalDirDestination):
            def __init__(self, root: Path) -> None:
                super().__init__(root, uri=str(root))
                self._puts = 0

            def put_blob(self, content_hash: str, data: bytes, *, worm: bool = False) -> None:
                self._puts += 1
                if self._puts > 1:
                    raise OSError("simulated network failure")
                super().put_blob(content_hash, data, worm=worm)

        selection = CapsuleSelection(capsules, query=None, query_resolved_at="2026-07-14T00:00:00Z")
        with pytest.raises(OSError):
            export_batch(selection, FailingDest(dest_dir), signer)
        # no manifest → known-incomplete (spec §Export 4/5)
        assert not _manifest_path(dest_dir).exists()

        # resume: re-run skips the blob that made it, writes the rest + manifest
        result = _export(capsules, dest_dir, signer)
        assert result.skipped == 1 and result.written == 1
        assert _manifest_path(dest_dir).exists()
        report = verify_export_manifest(_manifest_path(dest_dir), public_pem)
        assert report.status is VerifyStatus.VALID

    def test_worm_intent_recorded_and_local_blobs_read_only(
        self, capsule_root: Path, tmp_path: Path, signer: LocalSigner
    ) -> None:
        capsule = make_capsule(capsule_root, "run-a")
        dest = tmp_path / "out"
        worm = WormIntent(mode="compliance", retain_until="2033-07-12T00:00:00Z")
        result = _export([capsule], dest, signer, worm=worm)
        raw = json.loads(_manifest_path(dest).read_text())
        assert raw["worm"] == {"mode": "compliance", "retain_until": "2033-07-12T00:00:00Z"}
        blob = dest / blob_key(result.manifest.members[0].content_hash)
        assert (blob.stat().st_mode & 0o222) == 0  # not writable


class TestVerify:
    def _exported(
        self, capsule_root: Path, tmp_path: Path, signer: LocalSigner
    ) -> tuple[Path, ExportManifest]:
        capsules = [
            make_capsule(capsule_root, "run-a", content="A"),
            make_capsule(capsule_root, "run-b", content="B"),
        ]
        dest = tmp_path / "out"
        result = _export(capsules, dest, signer)
        return dest, result.manifest

    def test_round_trip_valid(
        self, capsule_root: Path, tmp_path: Path, signer: LocalSigner, public_pem: bytes
    ) -> None:
        dest, _ = self._exported(capsule_root, tmp_path, signer)
        report = verify_export_manifest(_manifest_path(dest), public_pem)
        assert report.status is VerifyStatus.VALID
        assert report.members_ok == report.members_total == 2
        assert report.problems == []

    def test_wrong_public_key_is_invalid(
        self, capsule_root: Path, tmp_path: Path, signer: LocalSigner
    ) -> None:
        from novafabric.evidence.signing import generate_keypair

        dest, _ = self._exported(capsule_root, tmp_path, signer)
        _, other_pub = generate_keypair(tmp_path / "otherkeys")
        report = verify_export_manifest(_manifest_path(dest), other_pub.read_bytes())
        assert report.status is VerifyStatus.INVALID

    def test_tampered_manifest_field_is_invalid(
        self, capsule_root: Path, tmp_path: Path, signer: LocalSigner, public_pem: bytes
    ) -> None:
        dest, _ = self._exported(capsule_root, tmp_path, signer)
        path = _manifest_path(dest)
        raw = json.loads(path.read_text())
        raw["dest"] = "s3://attacker-bucket/"
        path.write_text(json.dumps(raw))
        report = verify_export_manifest(path, public_pem)
        assert report.status is VerifyStatus.INVALID

    def test_dropped_member_is_invalid(
        self, capsule_root: Path, tmp_path: Path, signer: LocalSigner, public_pem: bytes
    ) -> None:
        """Quietly dropping member #2 of 2 must be detectable (ADR-0141 alt-2)."""
        dest, _ = self._exported(capsule_root, tmp_path, signer)
        path = _manifest_path(dest)
        raw = json.loads(path.read_text())
        raw["members"] = raw["members"][:1]
        raw["count"] = 1
        path.write_text(json.dumps(raw))
        report = verify_export_manifest(path, public_pem)
        assert report.status is VerifyStatus.INVALID

    def test_count_mismatch_is_invalid(
        self, capsule_root: Path, tmp_path: Path, signer: LocalSigner, public_pem: bytes
    ) -> None:
        dest, _ = self._exported(capsule_root, tmp_path, signer)
        path = _manifest_path(dest)
        raw = json.loads(path.read_text())
        raw["count"] = 3
        path.write_text(json.dumps(raw))
        report = verify_export_manifest(path, public_pem)
        assert report.status is VerifyStatus.INVALID

    def test_deleted_blob_is_incomplete(
        self, capsule_root: Path, tmp_path: Path, signer: LocalSigner, public_pem: bytes
    ) -> None:
        dest, manifest = self._exported(capsule_root, tmp_path, signer)
        (dest / blob_key(manifest.members[0].content_hash)).unlink()
        report = verify_export_manifest(_manifest_path(dest), public_pem)
        assert report.status is VerifyStatus.INCOMPLETE
        assert report.members_ok == 1
        assert any("missing" in p for p in report.problems)

    def test_modified_blob_is_incomplete(
        self, capsule_root: Path, tmp_path: Path, signer: LocalSigner, public_pem: bytes
    ) -> None:
        dest, manifest = self._exported(capsule_root, tmp_path, signer)
        blob = dest / blob_key(manifest.members[0].content_hash)
        blob.write_bytes(blob.read_bytes() + b"tampered")
        report = verify_export_manifest(_manifest_path(dest), public_pem)
        assert report.status is VerifyStatus.INCOMPLETE
        assert any("content mismatch" in p for p in report.problems)

    def test_lying_size_with_valid_resignature_is_incomplete(
        self, capsule_root: Path, tmp_path: Path, signer: LocalSigner, public_pem: bytes
    ) -> None:
        """A signer attesting a wrong size is caught by the byte re-check."""
        dest, _ = self._exported(capsule_root, tmp_path, signer)
        path = _manifest_path(dest)
        raw = json.loads(path.read_text())
        raw["members"][0]["size"] += 1
        manifest = ExportManifest.model_validate({**raw, "signature": raw["signature"]})
        manifest.batch_digest = compute_batch_digest(manifest.members)
        payload = canonical_signing_payload(
            schema_version=manifest.schema_version,
            export_id=manifest.export_id,
            dest=manifest.dest,
            members=manifest.members,
            count=manifest.count,
            batch_digest=manifest.batch_digest,
        )
        envelope = dsse_sign_payload(payload, MANIFEST_PAYLOAD_TYPE, signer)
        raw["batch_digest"] = manifest.batch_digest
        raw["signature"] = envelope
        path.write_text(json.dumps(raw))
        report = verify_export_manifest(path, public_pem)
        assert report.status is VerifyStatus.INCOMPLETE
        assert any("size mismatch" in p for p in report.problems)

    def test_embedded_payload_mismatch_is_invalid(
        self, capsule_root: Path, tmp_path: Path, signer: LocalSigner, public_pem: bytes
    ) -> None:
        dest, _ = self._exported(capsule_root, tmp_path, signer)
        path = _manifest_path(dest)
        raw = json.loads(path.read_text())
        raw["signature"]["payload"] = base64.b64encode(b"{}").decode()
        path.write_text(json.dumps(raw))
        report = verify_export_manifest(path, public_pem)
        assert report.status is VerifyStatus.INVALID

    def test_unreadable_manifest_is_invalid(self, tmp_path: Path, public_pem: bytes) -> None:
        bad = tmp_path / "manifest.json"
        bad.write_text("not json")
        report = verify_export_manifest(bad, public_pem)
        assert report.status is VerifyStatus.INVALID

    def test_moved_destination_falls_back_to_manifest_dir(
        self, capsule_root: Path, tmp_path: Path, signer: LocalSigner, public_pem: bytes
    ) -> None:
        dest, _ = self._exported(capsule_root, tmp_path, signer)
        moved = tmp_path / "moved"
        shutil.move(str(dest), str(moved))
        report = verify_export_manifest(_manifest_path(moved), public_pem)
        assert report.status is VerifyStatus.VALID

    def test_dest_override(
        self, capsule_root: Path, tmp_path: Path, signer: LocalSigner, public_pem: bytes
    ) -> None:
        dest, _ = self._exported(capsule_root, tmp_path, signer)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        manifest_copy = elsewhere / MANIFEST_FILENAME
        shutil.copy(_manifest_path(dest), manifest_copy)
        report = verify_export_manifest(
            manifest_copy, public_pem, dest_override=str(dest)
        )
        assert report.status is VerifyStatus.VALID


class TestSelection:
    def test_explicit_ids_and_paths(self, capsule_root: Path) -> None:
        c1 = make_capsule(capsule_root, "run-a")
        make_capsule(capsule_root, "run-b")
        selection = select_capsules(["run-b", str(c1)], root=capsule_root)
        assert [d.name for d in selection.capsule_dirs] == ["run-b", "run-a"]
        assert selection.query is None

    def test_unknown_ref_raises(self, capsule_root: Path) -> None:
        with pytest.raises(ExportSelectionError, match="not a valid capsule"):
            select_capsules(["nope"], root=capsule_root)

    def test_scan_all(self, capsule_root: Path) -> None:
        make_capsule(capsule_root, "run-a")
        make_capsule(capsule_root, "run-b")
        (capsule_root / "not-a-capsule").mkdir()
        selection = select_capsules(root=capsule_root)
        assert [d.name for d in selection.capsule_dirs] == ["run-a", "run-b"]
        assert selection.query == "*"

    def test_time_range_filter(self, capsule_root: Path) -> None:
        make_capsule(capsule_root, "early", created_at="2026-06-01T00:00:00Z")
        make_capsule(capsule_root, "mid", created_at="2026-07-02T12:00:00Z")
        make_capsule(capsule_root, "late", created_at="2026-08-01T00:00:00Z")
        selection = select_capsules(
            root=capsule_root, since="2026-07-01T00:00:00Z", until="2026-07-31T00:00:00Z"
        )
        assert [d.name for d in selection.capsule_dirs] == ["mid"]
        assert selection.query == (
            "created_at >= '2026-07-01T00:00:00Z' AND created_at <= '2026-07-31T00:00:00Z'"
        )

    def test_capsule_without_created_at_excluded_from_time_filter(
        self, capsule_root: Path
    ) -> None:
        capsule = make_capsule(capsule_root, "no-ts")
        (capsule / "capsule.yaml").write_text("run_id: no-ts\n")
        selection = select_capsules(root=capsule_root, since="2020-01-01")
        assert selection.capsule_dirs == []

    def test_explicit_refs_exclusive_with_time_filter(self, capsule_root: Path) -> None:
        make_capsule(capsule_root, "run-a")
        with pytest.raises(ExportSelectionError, match="mutually exclusive"):
            select_capsules(["run-a"], root=capsule_root, since="2026-01-01")

    def test_invalid_timestamps_raise(self, capsule_root: Path) -> None:
        with pytest.raises(ExportSelectionError, match="--since"):
            select_capsules(root=capsule_root, since="not-a-date")
        with pytest.raises(ExportSelectionError, match="--until"):
            select_capsules(root=capsule_root, until="also-bad")
