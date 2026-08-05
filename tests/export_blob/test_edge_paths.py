"""Edge/error paths: keyring signer, CLI failures, odd capsule metadata."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import novafabric.trust.keyring as keyring
from export_blob.helpers import make_capsule
from novafabric.cli.main import app
from novafabric.export_blob.models import MANIFEST_FILENAME, ExportManifest
from novafabric.export_blob.packing import capsule_identifier
from novafabric.export_blob.service import (
    CapsuleSelection,
    KeyringSigner,
    VerifyStatus,
    export_batch,
    select_capsules,
    verify_export_manifest,
)

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[2]
VALID_FIXTURE = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "spec"
    / "batch-blob-export"
    / "valid-s3-three-members.json"
)


class TestKeyringSigner:
    def test_sign_verify_round_trip_via_keyring(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsule_root: Path
    ) -> None:
        monkeypatch.setattr(keyring, "_KEYRING_DIR", tmp_path / "keyring")
        signer = KeyringSigner(identity="tester@example")
        assert signer.keyid.startswith("keyring:ed25519:")

        from novafabric.export_blob.destinations import LocalDirDestination

        capsule = make_capsule(capsule_root, "run-a")
        dest_dir = tmp_path / "out"
        selection = CapsuleSelection(
            [capsule], query=None, query_resolved_at="2026-07-14T00:00:00Z"
        )
        export_batch(selection, LocalDirDestination(dest_dir, uri=str(dest_dir)), signer)
        report = verify_export_manifest(dest_dir / MANIFEST_FILENAME, signer.public_pem)
        assert report.status is VerifyStatus.VALID


class TestCliErrorPaths:
    def test_bad_signing_key_exits_nonzero(
        self, capsule_root: Path, tmp_path: Path
    ) -> None:
        capsule = make_capsule(capsule_root, "run-a")
        bad_key = tmp_path / "bad.pem"
        bad_key.write_text("not a key")
        result = runner.invoke(
            app,
            ["export-blob", "--dest", str(tmp_path / "o"), "-c", str(capsule), "--key", str(bad_key)],
        )
        assert result.exit_code == 1
        assert "Failed to load signing key" in result.output

    def test_unwritable_dest_reports_resumable_failure(
        self, capsule_root: Path, tmp_path: Path
    ) -> None:
        from novafabric.evidence.signing import generate_keypair

        capsule = make_capsule(capsule_root, "run-a")
        priv, _ = generate_keypair(tmp_path / "keys")
        blocked = tmp_path / "blocked"
        blocked.write_text("a file, not a directory")
        result = runner.invoke(
            app,
            ["export-blob", "--dest", str(blocked), "-c", str(capsule), "--key", str(priv)],
        )
        assert result.exit_code == 1
        assert "resumable" in result.output

    def test_worm_flag_prints_retention(self, capsule_root: Path, tmp_path: Path) -> None:
        from novafabric.evidence.signing import generate_keypair

        capsule = make_capsule(capsule_root, "run-a")
        priv, _ = generate_keypair(tmp_path / "keys")
        dest = tmp_path / "out"
        result = runner.invoke(
            app,
            [
                "export-blob",
                "--dest",
                str(dest),
                "-c",
                str(capsule),
                "--key",
                str(priv),
                "--worm",
                "--worm-retention-days",
                "30",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "worm:" in result.output
        assert json.loads((dest / MANIFEST_FILENAME).read_text())["worm"]["mode"] == "compliance"


class TestVerifyEdges:
    def test_unresolvable_recorded_dest_is_incomplete(
        self, capsule_root: Path, tmp_path: Path, signer, public_pem: bytes
    ) -> None:
        """A manifest honestly recording an unreachable scheme → INCOMPLETE, not a crash."""
        from novafabric.export_blob.destinations import LocalDirDestination

        capsule = make_capsule(capsule_root, "run-a")
        dest_dir = tmp_path / "out"
        # uri deliberately points at a not-yet-implemented scheme
        dest = LocalDirDestination(dest_dir, uri="gcs://bucket/exports/")
        selection = CapsuleSelection(
            [capsule], query=None, query_resolved_at="2026-07-14T00:00:00Z"
        )
        export_batch(selection, dest, signer)
        # move the manifest away so the local fallback cannot kick in either
        manifest = tmp_path / "elsewhere.json"
        (dest_dir / MANIFEST_FILENAME).rename(manifest)
        report = verify_export_manifest(manifest, public_pem)
        assert report.status is VerifyStatus.INCOMPLETE
        assert any("destination unavailable" in p for p in report.problems)


class TestMoreEdges:
    def test_garbage_public_key_is_invalid(
        self, capsule_root: Path, tmp_path: Path, signer
    ) -> None:
        from novafabric.export_blob.destinations import LocalDirDestination

        capsule = make_capsule(capsule_root, "run-a")
        dest_dir = tmp_path / "out"
        selection = CapsuleSelection(
            [capsule], query=None, query_resolved_at="2026-07-14T00:00:00Z"
        )
        export_batch(selection, LocalDirDestination(dest_dir, uri=str(dest_dir)), signer)
        report = verify_export_manifest(dest_dir / MANIFEST_FILENAME, b"not a pem")
        assert report.status is VerifyStatus.INVALID
        assert any("signature check failed" in p for p in report.problems)

    def test_s3_get_blob_reraises_non_404(self) -> None:
        from export_blob.test_destinations import FakeS3Client
        from novafabric.export_blob.destinations import S3Destination

        class BrokenClient(FakeS3Client):
            def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
                raise RuntimeError("access denied")

        dest = S3Destination("audit-bucket", "", "s3://audit-bucket/", client=BrokenClient())
        with pytest.raises(RuntimeError, match="access denied"):
            dest.get_blob("sha256:" + "0" * 64)

    def test_cli_defaults_to_keyring_signer(
        self, capsule_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(keyring, "_KEYRING_DIR", tmp_path / "keyring")
        capsule = make_capsule(capsule_root, "run-a")
        dest = tmp_path / "out"
        pub_out = tmp_path / "export.pub.pem"
        result = runner.invoke(
            app,
            [
                "export-blob",
                "--dest",
                str(dest),
                "-c",
                str(capsule),
                "--public-key-out",
                str(pub_out),
            ],
        )
        assert result.exit_code == 0, result.output
        verify = runner.invoke(
            app, ["verify", str(dest / MANIFEST_FILENAME), "--public-key", str(pub_out)]
        )
        assert verify.exit_code == 0, verify.output


class TestOddCapsuleMetadata:
    def test_invalid_yaml_falls_back_to_dir_name(self, capsule_root: Path) -> None:
        capsule = make_capsule(capsule_root, "broken-yaml")
        (capsule / "capsule.yaml").write_text("run_id: [unclosed\n")
        assert capsule_identifier(capsule) == "broken-yaml"

    def test_non_dict_yaml_excluded_from_time_filter(self, capsule_root: Path) -> None:
        capsule = make_capsule(capsule_root, "list-yaml")
        (capsule / "capsule.yaml").write_text("- just\n- a list\n")
        selection = select_capsules(root=capsule_root, since="2020-01-01")
        assert selection.capsule_dirs == []

    def test_unparseable_yaml_excluded_from_time_filter(self, capsule_root: Path) -> None:
        capsule = make_capsule(capsule_root, "bad-yaml")
        (capsule / "capsule.yaml").write_text("created_at: [unclosed\n")
        selection = select_capsules(root=capsule_root, since="2020-01-01")
        assert selection.capsule_dirs == []


def test_detached_envelope_round_trips_to_json() -> None:
    """A fixture manifest (no embedded DSSE payload) serializes without a payload key."""
    manifest = ExportManifest.model_validate(json.loads(VALID_FIXTURE.read_text()))
    data = manifest.to_json_dict()
    assert "payload" not in data["signature"]
    assert data["count"] == 3
