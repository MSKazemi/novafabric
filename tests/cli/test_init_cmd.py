"""Tests for nova init command.

Covers:
- Creates NOVAFABRIC_HOME directory structure (capsules/, keys/, replays/)
- Generates Ed25519 keypair on first run
- Idempotent: second run skips key generation, exits 0
- --force regenerates keys even when they already exist
- CLI exits 0 on success, prints confirmation lines
- Respects --home flag to override NOVAFABRIC_HOME
"""
from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_pem_private_key,
)
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


class TestInitDirectories:
    def test_creates_home_directory(self, tmp_path: Path) -> None:
        home = tmp_path / "nova"
        result = runner.invoke(app, ["init", "--home", str(home)])
        assert result.exit_code == 0, result.output
        assert home.is_dir()

    def test_creates_capsules_subdir(self, tmp_path: Path) -> None:
        home = tmp_path / "nova"
        runner.invoke(app, ["init", "--home", str(home)])
        assert (home / "capsules").is_dir()

    def test_creates_keys_subdir(self, tmp_path: Path) -> None:
        home = tmp_path / "nova"
        runner.invoke(app, ["init", "--home", str(home)])
        assert (home / "keys").is_dir()

    def test_creates_replays_subdir(self, tmp_path: Path) -> None:
        home = tmp_path / "nova"
        runner.invoke(app, ["init", "--home", str(home)])
        assert (home / "replays").is_dir()


class TestInitKeypair:
    def test_generates_private_key_file(self, tmp_path: Path) -> None:
        home = tmp_path / "nova"
        runner.invoke(app, ["init", "--home", str(home)])
        assert (home / "keys" / "signing_key.pem").exists()

    def test_generates_public_key_file(self, tmp_path: Path) -> None:
        home = tmp_path / "nova"
        runner.invoke(app, ["init", "--home", str(home)])
        assert (home / "keys" / "signing_key.pub.pem").exists()

    def test_private_key_is_valid_ed25519(self, tmp_path: Path) -> None:
        home = tmp_path / "nova"
        runner.invoke(app, ["init", "--home", str(home)])
        pem = (home / "keys" / "signing_key.pem").read_bytes()
        key = load_pem_private_key(pem, password=None)
        assert isinstance(key, Ed25519PrivateKey)

    def test_public_key_matches_private(self, tmp_path: Path) -> None:
        home = tmp_path / "nova"
        runner.invoke(app, ["init", "--home", str(home)])
        priv_pem = (home / "keys" / "signing_key.pem").read_bytes()
        pub_pem = (home / "keys" / "signing_key.pub.pem").read_bytes()
        key = load_pem_private_key(priv_pem, password=None)
        assert isinstance(key, Ed25519PrivateKey)
        derived_pub = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        assert derived_pub == pub_pem

    def test_private_key_file_mode_is_restrictive(self, tmp_path: Path) -> None:
        home = tmp_path / "nova"
        runner.invoke(app, ["init", "--home", str(home)])
        key_path = home / "keys" / "signing_key.pem"
        mode = oct(key_path.stat().st_mode)[-3:]
        assert mode == "600", f"Expected 600, got {mode}"


class TestInitIdempotent:
    def test_second_run_exits_zero(self, tmp_path: Path) -> None:
        home = tmp_path / "nova"
        runner.invoke(app, ["init", "--home", str(home)])
        result = runner.invoke(app, ["init", "--home", str(home)])
        assert result.exit_code == 0, result.output

    def test_second_run_does_not_overwrite_keys(self, tmp_path: Path) -> None:
        home = tmp_path / "nova"
        runner.invoke(app, ["init", "--home", str(home)])
        original = (home / "keys" / "signing_key.pem").read_bytes()
        runner.invoke(app, ["init", "--home", str(home)])
        assert (home / "keys" / "signing_key.pem").read_bytes() == original

    def test_second_run_prints_already_initialized(self, tmp_path: Path) -> None:
        home = tmp_path / "nova"
        runner.invoke(app, ["init", "--home", str(home)])
        result = runner.invoke(app, ["init", "--home", str(home)])
        assert "already" in result.output.lower()


class TestInitForce:
    def test_force_regenerates_keys(self, tmp_path: Path) -> None:
        home = tmp_path / "nova"
        runner.invoke(app, ["init", "--home", str(home)])
        original = (home / "keys" / "signing_key.pem").read_bytes()
        runner.invoke(app, ["init", "--home", str(home), "--force"])
        assert (home / "keys" / "signing_key.pem").read_bytes() != original

    def test_force_exits_zero(self, tmp_path: Path) -> None:
        home = tmp_path / "nova"
        runner.invoke(app, ["init", "--home", str(home)])
        result = runner.invoke(app, ["init", "--home", str(home), "--force"])
        assert result.exit_code == 0, result.output


class TestInitOutput:
    def test_prints_home_path(self, tmp_path: Path) -> None:
        home = tmp_path / "nova"
        result = runner.invoke(app, ["init", "--home", str(home)])
        assert str(home) in result.output

    def test_prints_next_step_serve(self, tmp_path: Path) -> None:
        home = tmp_path / "nova"
        result = runner.invoke(app, ["init", "--home", str(home)])
        assert "serve" in result.output

    def test_prints_next_step_capture(self, tmp_path: Path) -> None:
        home = tmp_path / "nova"
        result = runner.invoke(app, ["init", "--home", str(home)])
        assert "capture" in result.output
