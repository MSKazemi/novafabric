"""ADR-0301 — `nova seal init`: local, self-asserted sealing identity and first run.

Acceptance criteria covered here (numbering follows ADR-0301):

1. a fresh home gets key + local CA + leaf + config with restrictive modes; a second
   run changes nothing;
2. a capsule sealed afterwards verifies as ``self-asserted``; pinning the local CA
   gives ``local-ca-pinned``; an unrelated operator CA gives ``ca-anchored``;
3. ``--force`` rotates key and leaf under the same CA, archives, logs ``key_rotation``,
   and old capsules keep verifying (CA-pinned too); ``--new-ca`` needs ``--force``;
4. an operator-managed ``novaseal.yaml`` is never replaced;
5. broken config / key-cert mismatch: ``seal init`` exits 1, capture warns and writes
   no ``.seal/`` but still writes the capsule;
6. everything works with networking disabled;
7. no RFC 3161 token ⇒ ``timestamp_ok`` is ``None`` everywhere (radar ``n/a``);
8. ``nova init`` offers the step, archives on ``--force``, refuses when the seal
   profile signs with its key.
"""

from __future__ import annotations

import datetime
import json
import socket
import sqlite3
from pathlib import Path

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from typer.testing import CliRunner

from novafabric.capture.orchestrator import _seal_capsule
from novafabric.cli.main import app
from novafabric.trust.novaseal.identity_trust import (
    IDENTITY_CA_ANCHORED,
    IDENTITY_LOCAL_CA_PINNED,
    IDENTITY_NONE,
    IDENTITY_SELF_ASSERTED,
    anchored_identity_trust,
    signer_certificate_info,
    unanchored_identity_trust,
)
from novafabric.trust.novaseal.local_identity import (
    LOCAL_IDENTITY_ORG,
    MANAGED_MARKER,
    LocalIdentityError,
    LocalIdentityPaths,
    init_local_identity,
)
from novafabric.trust.novaseal.x509_identity import validate_certificate_chain

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "nova-home"
    monkeypatch.setenv("NOVAFABRIC_HOME", str(root))
    monkeypatch.delenv("NOVAFABRIC_SEAL_CONFIG", raising=False)
    monkeypatch.delenv("NOVAFABRIC_SEAL_DB_PATH", raising=False)
    return root


def _paths(home: Path) -> LocalIdentityPaths:
    return LocalIdentityPaths(home)


def _seal_init(home: Path, *args: str) -> object:
    return runner.invoke(app, ["seal", "init", "--home", str(home), *args])


def _sealed_capsule(home: Path, name: str) -> Path:
    """Write a minimal capsule and seal it through the real capture sealing path."""
    capsule = home / "capsules" / name
    capsule.mkdir(parents=True)
    manifest = {"run_id": name, "status": "success"}
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    _seal_capsule(capsule, manifest)
    return capsule


def _verify_json(capsule: Path, *args: str) -> tuple[int, dict[str, object]]:
    result = runner.invoke(app, ["verify", str(capsule), "--json", *args])
    return result.exit_code, json.loads(result.output)


def _mode(path: Path) -> str:
    return oct(path.stat().st_mode)[-3:]


def _rotation_entries(db: Path) -> list[dict[str, object]]:
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT entry_json FROM leaves ORDER BY leaf_index").fetchall()
    finally:
        conn.close()
    return [e for e in (json.loads(r[0]) for r in rows) if e.get("event") == "key_rotation"]


def _operator_ca(tmp_path: Path) -> tuple[Path, Path, Path]:
    """An operator CA (no NovaFabric marker) + a P-256 leaf it issued. Returns paths."""
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Example Corp Signing CA")])
    ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(ca_ski, critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ml-platform")]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=10))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = tmp_path / "operator-ca.pem"
    key_path = tmp_path / "operator-leaf.key"
    cert_path = tmp_path / "operator-leaf.crt"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    return ca_path, key_path, cert_path


# ---------------------------------------------------------------------------
# AC1 — initialization
# ---------------------------------------------------------------------------


class TestInitialization:
    def test_creates_key_ca_leaf_and_config(self, home: Path) -> None:
        result = _seal_init(home)
        assert result.exit_code == 0, result.output
        p = _paths(home)
        for f in (p.signing_key, p.signing_cert, p.ca_key, p.ca_cert, p.config):
            assert f.is_file(), f
        assert "self-asserted" in result.output

    def test_private_keys_are_0600_and_directory_0700(self, home: Path) -> None:
        _seal_init(home)
        p = _paths(home)
        assert _mode(p.signing_key) == "600"
        assert _mode(p.ca_key) == "600"
        assert _mode(p.directory) == "700"

    def test_config_is_managed_local_and_offline(self, home: Path) -> None:
        _seal_init(home)
        p = _paths(home)
        text = p.config.read_text()
        assert text.splitlines()[0] == MANAGED_MARKER
        raw = yaml.safe_load(text)
        assert raw["profile"] == "local"
        assert "tsa_url" not in raw and "tsa_urls" not in raw  # no network by default

        from novafabric.trust.novaseal.config import load_signing_profile

        profile = load_signing_profile()
        assert profile is not None
        assert profile.profile == "local"
        assert profile.tsa_urls == []
        assert profile.key_path == p.signing_key

    def test_leaf_chains_to_local_ca_and_is_not_a_ca(self, home: Path) -> None:
        _seal_init(home)
        p = _paths(home)
        leaf = x509.load_pem_x509_certificate(p.signing_cert.read_bytes())
        ca = x509.load_pem_x509_certificate(p.ca_cert.read_bytes())
        assert validate_certificate_chain(leaf, [ca]).valid
        assert leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
        for cert in (leaf, ca):
            orgs = cert.subject.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
            assert [a.value for a in orgs] == [LOCAL_IDENTITY_ORG]

    def test_signing_key_is_p256_and_not_the_nova_init_key(self, home: Path) -> None:
        runner.invoke(app, ["init", "--home", str(home)])
        _seal_init(home)
        key = serialization.load_pem_private_key(
            _paths(home).signing_key.read_bytes(), password=None
        )
        assert isinstance(key, ec.EllipticCurvePrivateKey)
        assert isinstance(key.curve, ec.SECP256R1)
        assert (home / "keys" / "signing_key.pem").read_bytes() != _paths(
            home
        ).signing_key.read_bytes()

    def test_certificates_carry_no_hostname(self, home: Path) -> None:
        _seal_init(home)
        host = socket.gethostname()
        if len(host) < 4:
            pytest.skip("hostname too short to search for meaningfully")
        for f in (_paths(home).signing_cert, _paths(home).ca_cert):
            cert = x509.load_pem_x509_certificate(f.read_bytes())
            assert host not in cert.subject.rfc4514_string()

    def test_second_run_is_a_no_op(self, home: Path) -> None:
        _seal_init(home)
        p = _paths(home)
        before = {f: f.read_bytes() for f in (p.signing_key, p.signing_cert, p.ca_cert, p.config)}
        result = _seal_init(home)
        assert result.exit_code == 0, result.output
        assert "already configured" in result.output.lower()
        assert {f: f.read_bytes() for f in before} == before
        assert not p.archive_root.exists()

    def test_reuses_a_ca_and_key_left_in_place(self, home: Path) -> None:
        _seal_init(home)
        p = _paths(home)
        ca_before = p.ca_cert.read_bytes()
        key_before = p.signing_key.read_bytes()
        p.config.unlink()  # e.g. the user deleted the config only
        result = init_local_identity(home)
        assert result.status == "created"
        assert result.ca_reused and result.signing_key_reused
        assert p.ca_cert.read_bytes() == ca_before
        assert p.signing_key.read_bytes() == key_before

    def test_half_ca_is_archived_not_overwritten(self, home: Path) -> None:
        _seal_init(home)
        p = _paths(home)
        orphan_cert = p.ca_cert.read_bytes()
        p.ca_key.unlink()
        p.config.unlink()
        result = init_local_identity(home)
        assert result.archive_dir is not None
        assert (result.archive_dir / "ca.crt.pem").read_bytes() == orphan_cert
        assert p.ca_cert.read_bytes() != orphan_cert


# ---------------------------------------------------------------------------
# AC2 — sealing + verification trust labels
# ---------------------------------------------------------------------------


class TestSealAndVerify:
    def test_capture_seals_after_seal_init(self, home: Path) -> None:
        _seal_init(home)
        capsule = _sealed_capsule(home, "run-a")
        assert (capsule / ".seal" / "manifest.dsse").is_file()

    def test_capture_does_not_seal_before_seal_init(self, home: Path) -> None:
        runner.invoke(app, ["init", "--home", str(home)])
        capsule = _sealed_capsule(home, "run-a")
        assert not (capsule / ".seal").exists()  # opt-in: no silent default change

    def test_verify_reports_self_asserted(self, home: Path) -> None:
        _seal_init(home)
        capsule = _sealed_capsule(home, "run-a")
        code, data = _verify_json(capsule)
        assert code == 0, data
        assert data["valid"] is True
        assert data["identity_trust"] == IDENTITY_SELF_ASSERTED
        assert data["local_seal_identity"] is True
        assert data["ca_chain_checked"] is False and data["ca_chain_ok"] is None

        text = runner.invoke(app, ["verify", str(capsule)])
        assert text.exit_code == 0, text.output
        assert "SELF-ASSERTED" in text.output
        assert "identity_trust=self-asserted" in text.output
        assert "ca_chain_ok=False" in text.output  # no chain was validated

    def test_pinning_the_local_ca_reports_local_ca_pinned(self, home: Path) -> None:
        _seal_init(home)
        capsule = _sealed_capsule(home, "run-a")
        code, data = _verify_json(capsule, "--ca-bundle", str(_paths(home).ca_cert))
        assert code == 0, data
        assert data["identity_trust"] == IDENTITY_LOCAL_CA_PINNED
        assert data["ca_chain_ok"] is True

    def test_operator_ca_reports_ca_anchored(self, home: Path, tmp_path: Path) -> None:
        ca_path, key_path, cert_path = _operator_ca(tmp_path)
        home.mkdir(parents=True, exist_ok=True)
        (home / "novaseal.yaml").write_text(
            f"profile: local\nkey_path: {key_path}\ncert_path: {cert_path}\n"
            f"merkle_db: {home / 'm.db'}\n"
        )
        capsule = _sealed_capsule(home, "run-a")

        code, data = _verify_json(capsule)
        assert code == 0, data
        assert data["identity_trust"] == IDENTITY_SELF_ASSERTED  # issuer claim unchecked
        assert data["local_seal_identity"] is False
        text = runner.invoke(app, ["verify", str(capsule)]).output
        assert "unverified claim" in text

        code, data = _verify_json(capsule, "--ca-bundle", str(ca_path))
        assert code == 0, data
        assert data["identity_trust"] == IDENTITY_CA_ANCHORED

    def test_wrong_ca_bundle_fails_and_does_not_upgrade(
        self, home: Path, tmp_path: Path
    ) -> None:
        _seal_init(home)
        capsule = _sealed_capsule(home, "run-a")
        ca_path, _, _ = _operator_ca(tmp_path)
        code, data = _verify_json(capsule, "--ca-bundle", str(ca_path))
        assert code == 1
        assert data["identity_trust"] == IDENTITY_SELF_ASSERTED
        assert data["ca_chain_ok"] is False

    def test_tampered_signature_reports_none(self, home: Path) -> None:
        # Reversing padded base64 puts "=" first: before ADR-0301 this crashed
        # `nova verify` with binascii.Error instead of reporting a failed check.
        _seal_init(home)
        capsule = _sealed_capsule(home, "run-a")
        dsse = capsule / ".seal" / "manifest.dsse"
        env = json.loads(dsse.read_bytes())
        env["signatures"][0]["sig"] = env["signatures"][0]["sig"][::-1]
        dsse.write_text(json.dumps(env))
        code, data = _verify_json(capsule)
        assert code == 1
        assert data["identity_trust"] == IDENTITY_NONE

    def test_unsealed_capsule_json(self, home: Path) -> None:
        capsule = home / "capsules" / "plain"
        capsule.mkdir(parents=True)
        code, data = _verify_json(capsule)
        assert code == 1
        assert data["sealed"] is False and data["identity_trust"] == IDENTITY_NONE
        text = runner.invoke(app, ["verify", str(capsule)]).output
        assert "nova seal init" in " ".join(text.split())

    def test_json_is_refused_for_non_capsule_targets(self, tmp_path: Path) -> None:
        bundle = tmp_path / "b.zip"
        bundle.write_bytes(b"PK")
        result = runner.invoke(app, ["verify", str(bundle), "--json"])
        assert result.exit_code == 2


class TestIdentityTrustUnits:
    def test_marker_on_anchor_only_lowers_the_label(self, tmp_path: Path) -> None:
        ca_path, _, _ = _operator_ca(tmp_path)
        operator_ca = x509.load_pem_x509_certificate(ca_path.read_bytes())
        assert anchored_identity_trust(operator_ca) == IDENTITY_CA_ANCHORED
        assert anchored_identity_trust(None) == IDENTITY_LOCAL_CA_PINNED

    def test_unanchored_levels(self) -> None:
        assert unanchored_identity_trust(True) == IDENTITY_SELF_ASSERTED
        assert unanchored_identity_trust(False) == IDENTITY_NONE

    def test_signer_info_never_raises_on_garbage(self) -> None:
        assert signer_certificate_info(b"") is None
        assert signer_certificate_info(b"not json") is None
        assert signer_certificate_info(b'{"signatures":[{"cert":"!!"}]}') is None


# ---------------------------------------------------------------------------
# AC3 — rotation / regeneration
# ---------------------------------------------------------------------------


class TestRotation:
    def test_force_rotates_key_keeps_ca_archives_and_logs(self, home: Path) -> None:
        _seal_init(home)
        p = _paths(home)
        old_key = p.signing_key.read_bytes()
        old_cert = p.signing_cert.read_bytes()
        ca_before = p.ca_cert.read_bytes()

        result = _seal_init(home, "--force")
        assert result.exit_code == 0, result.output
        assert p.signing_key.read_bytes() != old_key
        assert p.ca_cert.read_bytes() == ca_before

        archives = list(p.archive_root.iterdir())
        assert len(archives) == 1
        assert (archives[0] / "signing.key.pem").read_bytes() == old_key
        assert (archives[0] / "signing.crt.pem").read_bytes() == old_cert
        assert (archives[0] / "novaseal.yaml").is_file()
        assert _mode(archives[0] / "signing.key.pem") == "600"

        entries = _rotation_entries(p.merkle_db)
        assert len(entries) == 1 and entries[0]["ca_replaced"] is False
        assert entries[0]["old_keyid"] != entries[0]["new_keyid"]

    def test_old_and_new_capsules_verify_ca_pinned_after_rotation(self, home: Path) -> None:
        _seal_init(home)
        before = _sealed_capsule(home, "before")
        _seal_init(home, "--force")
        after = _sealed_capsule(home, "after")
        ca = str(_paths(home).ca_cert)
        for capsule in (before, after):
            code, data = _verify_json(capsule, "--ca-bundle", ca)
            assert code == 0, data
            assert data["identity_trust"] == IDENTITY_LOCAL_CA_PINNED

    def test_new_ca_requires_force(self, home: Path) -> None:
        _seal_init(home)
        result = _seal_init(home, "--new-ca")
        assert result.exit_code == 1
        assert "--force" in result.output
        with pytest.raises(LocalIdentityError):
            init_local_identity(home, new_ca=True)

    def test_force_new_ca_breaks_pinned_continuity_visibly(self, home: Path) -> None:
        _seal_init(home)
        p = _paths(home)
        before = _sealed_capsule(home, "before")
        old_ca = p.ca_cert.read_bytes()

        result = _seal_init(home, "--force", "--new-ca")
        assert result.exit_code == 0, result.output
        assert "local seal CA was replaced" in " ".join(result.output.split())
        assert p.ca_cert.read_bytes() != old_ca
        archive = next(p.archive_root.iterdir())
        assert (archive / "ca.crt.pem").read_bytes() == old_ca
        assert _rotation_entries(p.merkle_db)[0]["ca_replaced"] is True

        # The old capsule still verifies on its own (cert embedded in the envelope)…
        assert _verify_json(before)[0] == 0
        # …but no longer chains to the *new* CA.
        code, data = _verify_json(before, "--ca-bundle", str(p.ca_cert))
        assert code == 1 and data["ca_chain_ok"] is False

    def test_force_without_ca_key_fails_and_names_new_ca(self, home: Path) -> None:
        _seal_init(home)
        _paths(home).ca_key.unlink()
        result = _seal_init(home, "--force")
        assert result.exit_code == 1
        assert "--new-ca" in result.output


# ---------------------------------------------------------------------------
# AC4 — operator-managed config is never replaced
# ---------------------------------------------------------------------------


class TestOperatorManagedConfig:
    def test_left_untouched_with_and_without_force(self, home: Path, tmp_path: Path) -> None:
        _, key_path, cert_path = _operator_ca(tmp_path)
        home.mkdir(parents=True)
        config = home / "novaseal.yaml"
        text = f"profile: local\nkey_path: {key_path}\ncert_path: {cert_path}\n"
        config.write_text(text)

        result = _seal_init(home)
        assert result.exit_code == 0, result.output
        assert "operator-managed" in result.output
        assert config.read_text() == text

        result = _seal_init(home, "--force")
        assert result.exit_code == 1
        assert config.read_text() == text
        assert not _paths(home).directory.exists()


# ---------------------------------------------------------------------------
# AC5 — missing / broken config: fail at setup, degrade at capture
# ---------------------------------------------------------------------------


class TestBrokenConfig:
    def test_broken_managed_config_exits_1_with_hint(self, home: Path) -> None:
        _seal_init(home)
        _paths(home).signing_cert.unlink()
        result = _seal_init(home)
        assert result.exit_code == 1
        assert "--force" in result.output

    def test_force_repairs_a_broken_managed_config(self, home: Path) -> None:
        _seal_init(home)
        p = _paths(home)
        ca_before = p.ca_cert.read_bytes()
        p.signing_cert.unlink()
        result = _seal_init(home, "--force")
        assert result.exit_code == 0, result.output
        assert p.ca_cert.read_bytes() == ca_before
        assert _rotation_entries(p.merkle_db)[0]["old_keyid"] == "unknown"
        capsule = _sealed_capsule(home, "repaired")
        assert _verify_json(capsule)[0] == 0

    def test_capture_warns_and_continues_on_broken_config(
        self, home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seal_init(home)
        _paths(home).signing_key.unlink()
        capsule = _sealed_capsule(home, "run-a")
        assert (capsule / "capsule.yaml").is_file()  # the workload's capsule survives
        assert not (capsule / ".seal").exists()
        assert "NovaSeal config error" in capsys.readouterr().err

    def test_capture_refuses_to_seal_with_a_mismatched_key(
        self, home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seal_init(home)
        other = ec.generate_private_key(ec.SECP256R1())
        _paths(home).signing_key.write_bytes(
            other.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        capsule = _sealed_capsule(home, "run-a")
        assert not (capsule / ".seal").exists()  # never a seal that cannot verify
        assert "does not match" in capsys.readouterr().err

    def test_a_non_p256_signing_key_left_in_place_is_refused(self, home: Path) -> None:
        p = _paths(home)
        p.directory.mkdir(parents=True)
        p.signing_key.write_text("not a key")
        result = _seal_init(home)
        assert result.exit_code == 1
        assert "P-256" in result.output

    def test_env_override_is_warned(
        self, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(tmp_path / "elsewhere.yaml"))
        result = _seal_init(home)
        assert result.exit_code == 0, result.output
        assert "NOVAFABRIC_SEAL_CONFIG" in result.output


# ---------------------------------------------------------------------------
# AC6 — air-gapped
# ---------------------------------------------------------------------------


class TestAirGapped:
    def test_init_seal_verify_open_no_socket(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_network(*_a: object, **_k: object) -> None:
            raise AssertionError("network access attempted in an air-gapped flow")

        monkeypatch.setattr(socket.socket, "connect", _no_network)
        monkeypatch.setattr(socket, "create_connection", _no_network)
        monkeypatch.setattr(socket, "getaddrinfo", _no_network)

        assert _seal_init(home).exit_code == 0
        capsule = _sealed_capsule(home, "offline")
        assert (capsule / ".seal" / "manifest.dsse").is_file()
        code, data = _verify_json(capsule, "--ca-bundle", str(_paths(home).ca_cert))
        assert code == 0, data
        assert data["timestamp_ok"] is None


# ---------------------------------------------------------------------------
# AC7 — timestamp absence is reported as absence (regression)
# ---------------------------------------------------------------------------


class TestTimestampAbsence:
    def test_absent_token_is_none_in_json_and_text(self, home: Path) -> None:
        _seal_init(home)
        capsule = _sealed_capsule(home, "run-a")
        code, data = _verify_json(capsule)
        assert code == 0
        assert data["timestamp_ok"] is None and data["timestamp_present"] is False
        text = runner.invoke(app, ["verify", str(capsule)]).output
        assert "timestamp_ok=True" not in text
        assert "timestamp_ok=None" in text

    def test_trust_radar_shows_na_not_ok(self, home: Path) -> None:
        from novafabric.trust.capsule_flags import flags_from_capsule
        from novafabric.trust.radar import build_trust_radar

        _seal_init(home)
        capsule = _sealed_capsule(home, "run-a")
        flags = flags_from_capsule(capsule)
        assert flags["signature_ok"] is True
        assert flags["timestamp_ok"] is None
        axis = next(a for a in build_trust_radar(flags).axes if a.key == "timestamp")
        assert axis.state.value == "na"

    def test_serve_verify_endpoint_reports_null_and_identity(
        self, home: Path, tmp_path: Path
    ) -> None:
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from novafabric.serve.app import create_app

        _seal_init(home)
        capsule = _sealed_capsule(home, "01TESTSEALINIT0000000000001")
        token = "test-token-1234567890abcdef"
        api = create_app(
            token=token,
            capsule_dir=capsule.parent,
            db_path=tmp_path / "registry.db",
            static_dir=None,
        )
        with TestClient(api) as client:
            res = client.post(
                f"/api/runs/{capsule.name}/verify",
                params={"token": token},
                headers={"host": "127.0.0.1:4321"},
            )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["valid"] is True
        assert body["timestamp_ok"] is None
        assert body["identity_trust"] == IDENTITY_SELF_ASSERTED
        assert body["local_seal_identity"] is True


# ---------------------------------------------------------------------------
# AC8 — `nova init` offers the step and never breaks continuity silently
# ---------------------------------------------------------------------------


class TestNovaInit:
    def test_offers_seal_init_without_running_it(self, home: Path) -> None:
        result = runner.invoke(app, ["init", "--home", str(home)])
        assert result.exit_code == 0, result.output
        assert "nova seal init" in result.output
        assert not (home / "novaseal.yaml").exists()

    def test_reports_configured_sealing(self, home: Path) -> None:
        runner.invoke(app, ["init", "--home", str(home)])
        _seal_init(home)
        result = runner.invoke(app, ["init", "--home", str(home)])
        assert "sealing: configured" in result.output.lower()

    def test_force_archives_the_old_pair(self, home: Path) -> None:
        runner.invoke(app, ["init", "--home", str(home)])
        old = (home / "keys" / "signing_key.pem").read_bytes()
        old_pub = (home / "keys" / "signing_key.pub.pem").read_bytes()
        result = runner.invoke(app, ["init", "--home", str(home), "--force"])
        assert result.exit_code == 0, result.output
        archive = next((home / "keys" / "archive").iterdir())
        assert (archive / "signing_key.pem").read_bytes() == old
        assert (archive / "signing_key.pub.pem").read_bytes() == old_pub
        assert "archived" in result.output
        assert _mode(home / "keys" / "signing_key.pem") == "600"

    def test_force_does_not_touch_the_seal_identity(self, home: Path) -> None:
        runner.invoke(app, ["init", "--home", str(home)])
        _seal_init(home)
        seal_key = _paths(home).signing_key.read_bytes()
        result = runner.invoke(app, ["init", "--home", str(home), "--force"])
        assert result.exit_code == 0, result.output
        assert _paths(home).signing_key.read_bytes() == seal_key

    def test_force_refuses_when_sealing_signs_with_that_key(self, home: Path) -> None:
        runner.invoke(app, ["init", "--home", str(home)])
        key = home / "keys" / "signing_key.pem"
        (home / "novaseal.yaml").write_text(
            f"profile: local\nkey_path: {key}\ncert_path: {home / 'some.crt'}\n"
        )
        before = key.read_bytes()
        result = runner.invoke(app, ["init", "--home", str(home), "--force"])
        assert result.exit_code == 1
        assert "nova seal init --force" in result.output
        assert key.read_bytes() == before
