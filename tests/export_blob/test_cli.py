"""CLI smoke tests: ``nova export-blob`` + ``nova verify <export-manifest.json>``."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from export_blob.helpers import make_capsule
from novafabric.cli.main import app
from novafabric.evidence.signing import generate_keypair
from novafabric.export_blob.models import MANIFEST_FILENAME

runner = CliRunner()


def _keys(tmp_path: Path) -> tuple[Path, Path]:
    return generate_keypair(tmp_path / "keys")


class TestExportBlobCli:
    def test_export_then_verify_round_trip(self, capsule_root: Path, tmp_path: Path) -> None:
        capsule = make_capsule(capsule_root, "run-a")
        priv, pub = _keys(tmp_path)
        dest = tmp_path / "out"

        result = runner.invoke(
            app,
            [
                "export-blob",
                "--dest",
                str(dest),
                "--capsule",
                str(capsule),
                "--key",
                str(priv),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Exported 1 capsule(s)" in result.output
        manifest = dest / MANIFEST_FILENAME
        assert manifest.is_file()

        verify = runner.invoke(
            app, ["verify", str(manifest), "--public-key", str(pub)]
        )
        assert verify.exit_code == 0, verify.output
        assert "VALID" in verify.output

    def test_verify_detects_tamper(self, capsule_root: Path, tmp_path: Path) -> None:
        capsule = make_capsule(capsule_root, "run-a")
        priv, pub = _keys(tmp_path)
        dest = tmp_path / "out"
        runner.invoke(
            app,
            ["export-blob", "--dest", str(dest), "-c", str(capsule), "--key", str(priv)],
        )
        manifest = dest / MANIFEST_FILENAME
        raw = json.loads(manifest.read_text())
        raw["count"] = 2
        raw["members"].append(dict(raw["members"][0], capsule_id="ghost"))
        manifest.write_text(json.dumps(raw))

        verify = runner.invoke(app, ["verify", str(manifest), "--public-key", str(pub)])
        assert verify.exit_code == 1
        assert "INVALID" in verify.output

    def test_verify_requires_public_key(self, capsule_root: Path, tmp_path: Path) -> None:
        capsule = make_capsule(capsule_root, "run-a")
        priv, _pub = _keys(tmp_path)
        dest = tmp_path / "out"
        runner.invoke(
            app,
            ["export-blob", "--dest", str(dest), "-c", str(capsule), "--key", str(priv)],
        )
        verify = runner.invoke(app, ["verify", str(dest / MANIFEST_FILENAME)])
        assert verify.exit_code == 1
        assert "--public-key" in verify.output

    def test_out_and_public_key_out(self, capsule_root: Path, tmp_path: Path) -> None:
        capsule = make_capsule(capsule_root, "run-a")
        priv, pub = _keys(tmp_path)
        dest = tmp_path / "out"
        manifest_copy = tmp_path / "copy" / "manifest.json"
        pub_out = tmp_path / "copy" / "export.pub.pem"
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
                "--out",
                str(manifest_copy),
                "--public-key-out",
                str(pub_out),
            ],
        )
        assert result.exit_code == 0, result.output
        assert json.loads(manifest_copy.read_text())["count"] == 1
        assert pub_out.read_bytes() == pub.read_bytes()

    def test_selection_error_exits_nonzero(self, tmp_path: Path) -> None:
        priv, _ = _keys(tmp_path)
        result = runner.invoke(
            app,
            [
                "export-blob",
                "--dest",
                str(tmp_path / "out"),
                "-c",
                "does-not-exist",
                "--capsule-dir",
                str(tmp_path / "empty"),
                "--key",
                str(priv),
            ],
        )
        assert result.exit_code == 1
        assert "not a valid capsule" in result.output

    def test_gcs_dest_reports_planned(self, capsule_root: Path, tmp_path: Path) -> None:
        capsule = make_capsule(capsule_root, "run-a")
        priv, _ = _keys(tmp_path)
        result = runner.invoke(
            app,
            ["export-blob", "--dest", "gcs://bucket/x", "-c", str(capsule), "--key", str(priv)],
        )
        assert result.exit_code == 1
        assert "planned" in result.output

    def test_time_range_scan_via_capsule_dir(self, capsule_root: Path, tmp_path: Path) -> None:
        make_capsule(capsule_root, "old", created_at="2026-01-01T00:00:00Z")
        make_capsule(capsule_root, "new", created_at="2026-07-10T00:00:00Z")
        priv, pub = _keys(tmp_path)
        dest = tmp_path / "out"
        result = runner.invoke(
            app,
            [
                "export-blob",
                "--dest",
                str(dest),
                "--capsule-dir",
                str(capsule_root),
                "--since",
                "2026-07-01T00:00:00Z",
                "--key",
                str(priv),
            ],
        )
        assert result.exit_code == 0, result.output
        raw = json.loads((dest / MANIFEST_FILENAME).read_text())
        assert [m["capsule_id"] for m in raw["members"]] == ["new"]
        assert raw["query"] == "created_at >= '2026-07-01T00:00:00Z'"

    def test_nova_verify_capsule_dir_path_unchanged(self, tmp_path: Path) -> None:
        """Regression: directories still take the NovaSeal capsule path."""
        result = runner.invoke(app, ["verify", str(tmp_path / "missing-capsule")])
        assert result.exit_code == 1
        assert "capsule directory not found" in result.output
