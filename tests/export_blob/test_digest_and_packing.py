"""Normative batch digest + deterministic packing (spec batch-blob-export-v0)."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from export_blob.helpers import make_capsule
from novafabric.export_blob.digest import canonical_signing_payload, compute_batch_digest
from novafabric.export_blob.models import ExportMember
from novafabric.export_blob.packing import CapsulePackError, capsule_identifier, pack_capsule

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "spec" / "batch-blob-export"


def _member(cid: str, hexseed: str, size: int) -> ExportMember:
    digest = hashlib.sha256(hexseed.encode()).hexdigest()
    return ExportMember(capsule_id=cid, content_hash=f"sha256:{digest}", size=size)


class TestBatchDigest:
    def test_empty_batch_is_sha256_of_empty_string(self) -> None:
        assert compute_batch_digest([]) == "sha256:" + hashlib.sha256(b"").hexdigest()

    def test_order_independent(self) -> None:
        a = _member("run-a", "x", 1)
        b = _member("run-b", "y", 2)
        c = _member("run-c", "z", 3)
        assert compute_batch_digest([a, b, c]) == compute_batch_digest([c, a, b])

    def test_membership_sensitive(self) -> None:
        a = _member("run-a", "x", 1)
        b = _member("run-b", "y", 2)
        assert compute_batch_digest([a, b]) != compute_batch_digest([a])
        assert compute_batch_digest([a]) != compute_batch_digest([b])

    def test_size_sensitive(self) -> None:
        assert compute_batch_digest([_member("a", "x", 1)]) != compute_batch_digest(
            [_member("a", "x", 2)]
        )

    @pytest.mark.parametrize(
        "fixture",
        sorted(FIXTURES.glob("valid-*.json")),
        ids=lambda p: p.stem,
    )
    def test_reproduces_golden_fixture_digests(self, fixture: Path) -> None:
        """The valid golden fixtures carry REAL recomputed digests (spec AC-3)."""
        data = json.loads(fixture.read_text())
        members = [ExportMember.model_validate(m) for m in data["members"]]
        assert compute_batch_digest(members) == data["batch_digest"]

    def test_canonical_payload_is_deterministic_and_complete(self) -> None:
        a = _member("run-a", "x", 1)
        payload = canonical_signing_payload(
            schema_version="0.1.0",
            export_id="0" * 26,
            dest="./out",
            members=[a],
            count=1,
            batch_digest=compute_batch_digest([a]),
        )
        obj = json.loads(payload)
        assert set(obj) == {
            "schema_version",
            "export_id",
            "dest",
            "members",
            "count",
            "batch_digest",
        }
        assert obj["members"][0]["capsule_id"] == "run-a"


class TestPacking:
    def test_deterministic_across_repacks(self, capsule_root: Path) -> None:
        capsule = make_capsule(capsule_root, "run-1")
        assert pack_capsule(capsule) == pack_capsule(capsule)

    def test_identical_content_identical_bytes(self, capsule_root: Path) -> None:
        c1 = make_capsule(capsule_root, "same", with_run_id=False)
        other_root = capsule_root / "elsewhere"
        other_root.mkdir()
        c2 = make_capsule(other_root, "same", with_run_id=False)
        assert pack_capsule(c1) == pack_capsule(c2)

    def test_content_change_changes_bytes(self, capsule_root: Path) -> None:
        capsule = make_capsule(capsule_root, "run-1")
        before = pack_capsule(capsule)
        (capsule / "outputs" / "stdout.txt").write_text("changed")
        assert pack_capsule(capsule) != before

    def test_archive_contains_capsule_files(self, capsule_root: Path) -> None:
        capsule = make_capsule(capsule_root, "run-1")
        with tarfile.open(fileobj=io.BytesIO(pack_capsule(capsule))) as tf:
            names = tf.getnames()
        assert "capsule.yaml" in names
        assert "outputs/stdout.txt" in names

    def test_source_never_mutated(self, capsule_root: Path) -> None:
        capsule = make_capsule(capsule_root, "run-1")
        snapshot = {
            p.relative_to(capsule).as_posix(): p.read_bytes()
            for p in capsule.rglob("*")
            if p.is_file()
        }
        pack_capsule(capsule)
        after = {
            p.relative_to(capsule).as_posix(): p.read_bytes()
            for p in capsule.rglob("*")
            if p.is_file()
        }
        assert after == snapshot

    def test_not_a_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(CapsulePackError):
            pack_capsule(tmp_path / "missing")

    def test_capsule_identifier_prefers_run_id(self, capsule_root: Path) -> None:
        capsule = make_capsule(capsule_root, "dir-name")
        (capsule / "capsule.yaml").write_text("run_id: custom-id\ncreated_at: 2026-07-01T00:00:00Z\n")
        assert capsule_identifier(capsule) == "custom-id"
        no_id = make_capsule(capsule_root, "fallback", with_run_id=False)
        assert capsule_identifier(no_id) == "fallback"
