"""``nova verify --ca-bundle`` — ADR-0055 signer chain validation on the CLI (experimental).

Acceptance criteria:

* Without ``--ca-bundle`` (and no ``ca_bundle`` in novaseal.yaml) output and exit code
  are unchanged — no chain line is printed.
* A capsule sealed by a CA-issued leaf passes when the bundle reaches its anchor
  (intermediate supplied in the bundle), exit 0.
* Wrong CA, root-only bundle (missing intermediate), malformed bundle, unreadable
  bundle path and a bare-key envelope all fail closed with exit 1.
* A forged envelope that pairs the legitimate CA-issued leaf with an attacker key and
  signature (both reviewer PoC variants) fails the chain check: trust requires the
  signature to verify under the key of the certificate that chain-validates.
* ``ca_bundle`` from novaseal.yaml is honoured; ``--ca-bundle`` overrides it.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.trust.novaseal import KeyConfig, NovaSeal

from ._x509_pki import Pki, attacker_entry, ecdsa_entry, make_pki

runner = CliRunner()


@pytest.fixture()
def ca_sealed(tmp_path: Path) -> tuple[Path, Path, Pki]:
    """A capsule sealed with a CA-issued leaf. Returns (capsule_dir, config, pki)."""
    pki = make_pki("cli")
    key_path = tmp_path / "seal.key"
    cert_path = tmp_path / "seal.crt"
    key_path.write_bytes(pki.leaf.key_pem)
    cert_path.write_bytes(pki.leaf.cert_pem)
    merkle_db = tmp_path / "merkle.db"
    config_path = tmp_path / "novaseal.yaml"
    config_path.write_text(
        f"profile: local\nkey_path: {key_path}\ncert_path: {cert_path}\n"
        f"tsa_url: \nmerkle_db: {merkle_db}\n"
    )
    seal = NovaSeal(
        config=KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path)),
        tsa_url="",
        db_path=str(merkle_db),
    )
    bundle = seal.seal({"run_id": "ca-bundle-001", "status": "success"})
    capsule_dir = tmp_path / "ca-capsule"
    seal_dir = capsule_dir / ".seal"
    seal_dir.mkdir(parents=True)
    (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal_dir / "manifest.dsse.tsr").write_bytes(bundle.tsr)
    (seal_dir / "log-entry.json").write_text(json.dumps(bundle.log_entry), encoding="utf-8")
    return capsule_dir, config_path, pki


def _invoke(capsule_dir: Path, config: Path, *extra: str) -> tuple[int, str]:
    result = runner.invoke(app, ["verify", str(capsule_dir), "--seal-config", str(config), *extra])
    # Rich wraps long reasons at the terminal width; collapse whitespace so substring
    # assertions do not depend on where a line happened to break.
    return result.exit_code, " ".join(result.output.split())


def _bundle(tmp_path: Path, name: str, pem: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(pem)
    return path


def test_no_bundle_leaves_output_unchanged(ca_sealed: tuple[Path, Path, Pki]) -> None:
    capsule_dir, config, _ = ca_sealed
    code, out = _invoke(capsule_dir, config)
    assert code == 0, out
    assert "Signer certificate chain" not in out


def test_valid_chain_passes(tmp_path: Path, ca_sealed: tuple[Path, Path, Pki]) -> None:
    capsule_dir, config, pki = ca_sealed
    bundle = _bundle(tmp_path, "ca.pem", pki.root.cert_pem + pki.intermediate.cert_pem)
    code, out = _invoke(capsule_dir, config, "--ca-bundle", str(bundle))
    assert code == 0, out
    assert "Signer certificate chain (CA bundle)" in out
    # Every bundle cert is an anchor, so the shortest path ends at the intermediate.
    assert "CN=cli-signer <- CN=cli-intermediate-ca" in out


def test_root_only_bundle_missing_intermediate_fails(
    tmp_path: Path, ca_sealed: tuple[Path, Path, Pki]
) -> None:
    capsule_dir, config, pki = ca_sealed
    bundle = _bundle(tmp_path, "root.pem", pki.root.cert_pem)
    code, out = _invoke(capsule_dir, config, "--ca-bundle", str(bundle))
    assert code == 1
    assert "chain validation failed" in out


def test_wrong_ca_fails(tmp_path: Path, ca_sealed: tuple[Path, Path, Pki]) -> None:
    capsule_dir, config, _ = ca_sealed
    other = make_pki("other")
    bundle = _bundle(tmp_path, "other.pem", other.root.cert_pem + other.intermediate.cert_pem)
    code, out = _invoke(capsule_dir, config, "--ca-bundle", str(bundle))
    assert code == 1
    assert "Signer certificate chain (CA bundle)" in out


def test_malformed_bundle_fails(tmp_path: Path, ca_sealed: tuple[Path, Path, Pki]) -> None:
    capsule_dir, config, _ = ca_sealed
    bundle = _bundle(tmp_path, "junk.pem", b"not a certificate")
    code, out = _invoke(capsule_dir, config, "--ca-bundle", str(bundle))
    assert code == 1
    assert "could not load CA bundle" in out


def test_unreadable_bundle_fails(tmp_path: Path, ca_sealed: tuple[Path, Path, Pki]) -> None:
    capsule_dir, config, _ = ca_sealed
    code, out = _invoke(capsule_dir, config, "--ca-bundle", str(tmp_path / "absent.pem"))
    assert code == 1
    assert "cannot read CA bundle" in out


def test_config_ca_bundle_is_honoured_and_flag_overrides(
    tmp_path: Path, ca_sealed: tuple[Path, Path, Pki]
) -> None:
    capsule_dir, config, pki = ca_sealed
    wrong = make_pki("wrong")
    wrong_bundle = _bundle(tmp_path, "wrong.pem", wrong.root.cert_pem)
    config.write_text(config.read_text() + f"ca_bundle: {wrong_bundle}\n")
    code, out = _invoke(capsule_dir, config)
    assert code == 1
    assert "Signer certificate chain (CA bundle)" in out

    good = _bundle(tmp_path, "good.pem", pki.intermediate.cert_pem)
    code, out = _invoke(capsule_dir, config, "--ca-bundle", str(good))
    assert code == 0, out


def test_bare_key_envelope_fails_closed(tmp_path: Path, ca_sealed: tuple[Path, Path, Pki]) -> None:
    capsule_dir, config, pki = ca_sealed
    dsse = capsule_dir / ".seal" / "manifest.dsse"
    env = json.loads(dsse.read_bytes())
    env["signatures"][0].pop("cert", None)
    dsse.write_text(json.dumps(env))
    bundle = _bundle(tmp_path, "ca.pem", pki.intermediate.cert_pem)
    code, out = _invoke(capsule_dir, config, "--ca-bundle", str(bundle))
    assert code == 1
    assert "no X.509 certificate" in out


def _payload(env: dict[str, object]) -> bytes:
    raw = str(env["payload"])
    return base64.b64decode(raw.replace("-", "+").replace("_", "/") + "=" * (-len(raw) % 4))


def test_poc_attacker_pubkey_with_legit_cert_fails(
    tmp_path: Path, ca_sealed: tuple[Path, Path, Pki]
) -> None:
    """PoC 1: {pubkey: attacker, cert: legit leaf DER, sig: attacker}."""
    capsule_dir, config, pki = ca_sealed
    dsse = capsule_dir / ".seal" / "manifest.dsse"
    env = json.loads(dsse.read_bytes())
    env["signatures"] = [attacker_entry(_payload(env), pki.leaf.cert)]
    dsse.write_text(json.dumps(env))
    bundle = _bundle(tmp_path, "ca.pem", pki.intermediate.cert_pem)
    code, out = _invoke(capsule_dir, config, "--ca-bundle", str(bundle))
    assert code == 1
    assert "Signer certificate chain (CA bundle): FAIL" in out
    assert "'pubkey' does not match the certificate's public key" in out


def test_poc_legit_cert_garbage_sig_plus_attacker_entry_fails(
    tmp_path: Path, ca_sealed: tuple[Path, Path, Pki]
) -> None:
    """PoC 2: sig[0] legit cert + garbage sig; sig[1] attacker pubkey with a valid sig."""
    capsule_dir, config, pki = ca_sealed
    dsse = capsule_dir / ".seal" / "manifest.dsse"
    env = json.loads(dsse.read_bytes())
    legit = ecdsa_entry(pki.leaf, _payload(env))
    legit["sig"] = base64.b64encode(b"garbage").decode()
    env["signatures"] = [legit, attacker_entry(_payload(env), None)]
    dsse.write_text(json.dumps(env))
    bundle = _bundle(tmp_path, "ca.pem", pki.intermediate.cert_pem)
    code, out = _invoke(capsule_dir, config, "--ca-bundle", str(bundle))
    assert code == 1
    assert "Signer certificate chain (CA bundle): FAIL" in out
    assert "signatures[0]: signature does not verify" in out
