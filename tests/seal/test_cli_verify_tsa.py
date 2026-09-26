"""``nova verify --tsa-ca-bundle`` and ``tsa_ca_certs`` — ADR-0070 §1/§5 (experimental).

Acceptance criteria:

* Without ``--tsa-ca-bundle`` / ``tsa_ca_certs`` the output is unchanged (no TSA block).
* A capsule whose ``manifest.dsse.tsr`` is signed by a TSA chaining to the bundle,
  with a message imprint over the DSSE envelope → exit 0, genTime + chain printed.
* Wrong TSA CA, missing EKU, a token for other data, or a revoked TSA certificate
  (``--crl-dir``) → exit 1 with the reason shown.
* With TSA anchors given (``--tsa-ca-bundle``) or configured (``tsa_ca_certs``), an
  absent or empty ``manifest.dsse.tsr`` FAILS (exit 1): the token is outside the DSSE
  signature, so a key holder backdating a capsule could simply delete it. Without
  anchors a missing token stays the soft ``NOT PRESENT`` notice (exit 0).
* ``--crl-dir`` is accepted with only a TSA bundle; an unreadable bundle fails closed.
* ``tsa_ca_certs`` in novaseal.yaml is honoured; malformed values are config errors;
  ``crl_dir`` with only ``tsa_ca_certs`` is accepted.
* Evidence Bundle ZIPs: ``--tsa-ca-bundle`` verifies ``manifest.dsse.tsr`` against
  SHA-256 of ``attestations/run.intoto.json``.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from cryptography import x509
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.trust.novaseal import KeyConfig, NovaSeal
from novafabric.trust.novaseal.config import SealConfigError, _parse_profile

from ._tsa_pki import TokenSpec, TsaNode, make_tsa_cert, make_tsr
from ._x509_pki import NOW, Node, Revoked, crl_der, make_cert, make_crl, make_pki

runner = CliRunner()
DAY = datetime.timedelta(days=1)


@pytest.fixture()
def tsa_ca() -> Node:
    return make_cert("cli-tsa-root", issuer=None, ca=True)


@pytest.fixture()
def tsa(tsa_ca: Node) -> TsaNode:
    return make_tsa_cert(tsa_ca, cn="cli-tsa")


@pytest.fixture()
def sealed(tmp_path: Path) -> tuple[Path, Path, bytes]:
    """(capsule_dir, config, dsse_bytes) for a capsule sealed without a TSA."""
    pki = make_pki("clitsa")
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
    bundle = seal.seal({"run_id": "tsa-001", "status": "success"})
    seal_dir = tmp_path / "tsa-capsule" / ".seal"
    seal_dir.mkdir(parents=True)
    (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal_dir / "log-entry.json").write_text(json.dumps(bundle.log_entry), encoding="utf-8")
    return seal_dir.parent, config_path, bundle.dsse_envelope


def _stamp(capsule_dir: Path, tsa: TsaNode, dsse: bytes, **spec: object) -> None:
    token = make_tsr(tsa, TokenSpec(digest=hashlib.sha256(dsse).digest(), **spec))  # type: ignore[arg-type]
    (capsule_dir / ".seal" / "manifest.dsse.tsr").write_bytes(token)


def _pem(tmp_path: Path, node: Node, name: str = "tsa-ca.pem") -> Path:
    path = tmp_path / name
    path.write_bytes(node.cert_pem)
    return path


def _invoke(capsule_dir: Path, config: Path, *extra: str) -> tuple[int, str]:
    result = runner.invoke(app, ["verify", str(capsule_dir), "--seal-config", str(config), *extra])
    return result.exit_code, " ".join(result.output.split())


def test_default_output_unchanged(sealed: tuple[Path, Path, bytes], tsa: TsaNode) -> None:
    capsule_dir, config, dsse = sealed
    _stamp(capsule_dir, tsa, dsse)
    code, out = _invoke(capsule_dir, config)
    assert code == 0, out
    assert "TSA certificate chain" not in out


def test_good_token_passes(
    tmp_path: Path, sealed: tuple[Path, Path, bytes], tsa: TsaNode, tsa_ca: Node
) -> None:
    capsule_dir, config, dsse = sealed
    _stamp(capsule_dir, tsa, dsse)
    code, out = _invoke(capsule_dir, config, "--tsa-ca-bundle", str(_pem(tmp_path, tsa_ca)))
    assert code == 0, out
    assert "✓ TSA certificate chain (RFC 3161, TSA CA bundle)" in out
    assert "genTime:" in out and "TSA signer: CN=cli-tsa" in out
    assert "chain: CN=cli-tsa <- CN=cli-tsa-root" in out


def test_wrong_ca_fails(tmp_path: Path, sealed: tuple[Path, Path, bytes], tsa: TsaNode) -> None:
    capsule_dir, config, dsse = sealed
    _stamp(capsule_dir, tsa, dsse)
    other = make_cert("unrelated-root", issuer=None, ca=True)
    code, out = _invoke(capsule_dir, config, "--tsa-ca-bundle", str(_pem(tmp_path, other)))
    assert code == 1
    assert "✗ TSA certificate chain" in out and "chain validation failed" in out


def test_missing_eku_fails(tmp_path: Path, sealed: tuple[Path, Path, bytes], tsa_ca: Node) -> None:
    capsule_dir, config, dsse = sealed
    _stamp(capsule_dir, make_tsa_cert(tsa_ca, eku=None), dsse)
    code, out = _invoke(capsule_dir, config, "--tsa-ca-bundle", str(_pem(tmp_path, tsa_ca)))
    assert code == 1
    assert "no extendedKeyUsage" in out


def test_token_for_other_data_fails(
    tmp_path: Path, sealed: tuple[Path, Path, bytes], tsa: TsaNode, tsa_ca: Node
) -> None:
    capsule_dir, config, _ = sealed
    _stamp(capsule_dir, tsa, b"some other envelope")
    code, out = _invoke(capsule_dir, config, "--tsa-ca-bundle", str(_pem(tmp_path, tsa_ca)))
    assert code == 1
    assert "messageImprint does not match" in out


def test_revoked_tsa_cert_fails_with_crl_dir(
    tmp_path: Path, sealed: tuple[Path, Path, bytes], tsa: TsaNode, tsa_ca: Node
) -> None:
    capsule_dir, config, dsse = sealed
    _stamp(capsule_dir, tsa, dsse)
    crls = tmp_path / "crls"
    crls.mkdir()
    revoked = Revoked(tsa.cert.serial_number, NOW - DAY, x509.ReasonFlags.key_compromise)
    (crls / "root.crl").write_bytes(crl_der(make_crl(tsa_ca, (revoked,), this_update=NOW - DAY)))
    bundle = str(_pem(tmp_path, tsa_ca))
    code, out = _invoke(capsule_dir, config, "--tsa-ca-bundle", bundle, "--crl-dir", str(crls))
    assert code == 1
    assert "REVOKED CN=cli-tsa" in out and "key_compromise" in out


def test_missing_token_fails_when_tsa_anchors_given(
    tmp_path: Path, sealed: tuple[Path, Path, bytes], tsa_ca: Node
) -> None:
    capsule_dir, config, _ = sealed
    bundle = str(_pem(tmp_path, tsa_ca))
    code, out = _invoke(capsule_dir, config, "--tsa-ca-bundle", bundle)
    assert code == 1, out
    assert "✗ TSA certificate chain" in out
    assert "no RFC 3161 token (manifest.dsse.tsr) although TSA trust anchors" in out
    assert "NOT CHECKED" not in out
    (capsule_dir / ".seal" / "manifest.dsse.tsr").write_bytes(b"")
    code, out = _invoke(capsule_dir, config, "--tsa-ca-bundle", bundle)
    assert code == 1, out
    assert "is empty although TSA trust anchors are configured" in out


def test_missing_token_fails_when_tsa_ca_certs_configured(
    tmp_path: Path, sealed: tuple[Path, Path, bytes], tsa_ca: Node
) -> None:
    capsule_dir, config, _ = sealed
    config.write_text(config.read_text() + f"tsa_ca_certs:\n  - {_pem(tmp_path, tsa_ca)}\n")
    code, out = _invoke(capsule_dir, config)
    assert code == 1, out
    assert "no RFC 3161 token" in out


def test_missing_token_without_tsa_anchors_stays_soft(
    sealed: tuple[Path, Path, bytes],
) -> None:
    capsule_dir, config, _ = sealed
    code, out = _invoke(capsule_dir, config)
    assert code == 0, out
    assert "NOT PRESENT" in out and "TSA certificate chain" not in out


def test_unreadable_bundle_fails_closed(
    tmp_path: Path, sealed: tuple[Path, Path, bytes], tsa: TsaNode, tsa_ca: Node
) -> None:
    capsule_dir, config, dsse = sealed
    _stamp(capsule_dir, tsa, dsse)
    bundle = _pem(tmp_path, tsa_ca)
    config.write_text(config.read_text() + f"tsa_ca_certs:\n  - {bundle}\n")
    bundle.unlink()
    # The config loader refuses a configured-but-missing anchor file.
    code, out = _invoke(capsule_dir, config)
    assert code == 1 and "tsa_ca_certs file not found" in out


def test_config_tsa_ca_certs_honoured(
    tmp_path: Path, sealed: tuple[Path, Path, bytes], tsa: TsaNode, tsa_ca: Node
) -> None:
    capsule_dir, config, dsse = sealed
    _stamp(capsule_dir, tsa, dsse)
    crls = tmp_path / "crls"
    crls.mkdir()
    (crls / "root.crl").write_bytes(crl_der(make_crl(tsa_ca, this_update=NOW - DAY)))
    config.write_text(
        config.read_text()
        + f"tsa_ca_certs:\n  - {_pem(tmp_path, tsa_ca)}\ncrl_dir: {crls}\ncrl_strict: true\n"
    )
    code, out = _invoke(capsule_dir, config)
    assert code == 0, out
    assert "✓ TSA certificate chain" in out
    assert "Revocation (CRL, offline, strict)" in out and "GOOD CN=cli-tsa" in out


def test_crl_dir_accepted_with_only_tsa_bundle(
    tmp_path: Path, sealed: tuple[Path, Path, bytes], tsa: TsaNode, tsa_ca: Node
) -> None:
    capsule_dir, config, dsse = sealed
    _stamp(capsule_dir, tsa, dsse)
    crls = tmp_path / "crls"
    crls.mkdir()
    bundle = str(_pem(tmp_path, tsa_ca))
    code, out = _invoke(capsule_dir, config, "--tsa-ca-bundle", bundle, "--crl-dir", str(crls))
    assert code == 0, out
    assert "WARNING no_crl" in out
    code, out = _invoke(capsule_dir, config, "--crl-dir", str(crls))
    assert code == 2 and "--tsa-ca-bundle" in out


def test_tsa_chain_check_unreadable_anchor(tmp_path: Path) -> None:
    from novafabric.cli.verify import _tsa_chain_check

    ok, reason, lines = _tsa_chain_check(b"x", b"", [tmp_path / "missing.pem"])
    assert ok is False and "cannot read TSA CA bundle" in reason and lines == []


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("tsa_ca_certs: /one/path.pem\n", "non-empty list"),
        ("tsa_ca_certs: []\n", "non-empty list"),
        ("tsa_ca_certs:\n  - ''\n", "non-empty list"),
        ("tsa_ca_certs:\n  - /no/such/tsa.pem\n", "file not found"),
    ],
)
def test_config_rejects_bad_tsa_ca_certs(tmp_path: Path, value: str, message: str) -> None:
    key = tmp_path / "k.pem"
    cert = tmp_path / "c.pem"
    key.write_text("k")
    cert.write_text("c")
    cfg = tmp_path / "novaseal.yaml"
    cfg.write_text(
        f"profile: local\nkey_path: {key}\ncert_path: {cert}\n"
        f"merkle_db: {tmp_path / 'm.db'}\n{value}"
    )
    with pytest.raises(SealConfigError, match=message):
        _parse_profile(cfg)


def test_config_bounds_tsa_ca_certs(tmp_path: Path) -> None:
    pem = tmp_path / "a.pem"
    pem.write_text("x")
    key = tmp_path / "k.pem"
    key.write_text("k")
    cfg = tmp_path / "novaseal.yaml"
    listing = "".join(f"  - {pem}\n" for _ in range(33))
    cfg.write_text(
        f"profile: local\nkey_path: {key}\ncert_path: {key}\n"
        f"merkle_db: {tmp_path / 'm.db'}\ntsa_ca_certs:\n{listing}"
    )
    with pytest.raises(SealConfigError, match="more than 32"):
        _parse_profile(cfg)
    cfg.write_text(
        f"profile: local\nkey_path: {key}\ncert_path: {key}\n"
        f"merkle_db: {tmp_path / 'm.db'}\ntsa_ca_certs:\n  - {pem}\n"
    )
    assert _parse_profile(cfg).tsa_ca_certs == [pem]


# ---------------------------------------------------------------------------
# Evidence Bundle ZIP
# ---------------------------------------------------------------------------


def _bundle(tmp_path: Path, tsr: bytes | None, attestation: bytes | None = b"{}") -> Path:
    files: dict[str, bytes] = {}
    if attestation is not None:
        files["attestations/run.intoto.json"] = attestation
    if tsr is not None:
        files["manifest.dsse.tsr"] = tsr
    artifacts = [
        {"path": p, "sha256": "sha256:" + hashlib.sha256(b).hexdigest()} for p, b in files.items()
    ]
    path = tmp_path / "evidence.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"bundle_id": "b1", "artifacts": artifacts}))
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def _zip_invoke(path: Path, *extra: str) -> tuple[int, str]:
    result = runner.invoke(app, ["verify", str(path), *extra])
    return result.exit_code, " ".join(result.output.split())


def test_evidence_bundle_tsa_check(tmp_path: Path, tsa: TsaNode, tsa_ca: Node) -> None:
    attestation = b'{"payload": "x"}'
    good = make_tsr(tsa, TokenSpec(digest=hashlib.sha256(attestation).digest()))
    bundle = str(_pem(tmp_path, tsa_ca))
    code, out = _zip_invoke(_bundle(tmp_path, good, attestation), "--tsa-ca-bundle", bundle)
    assert code == 0, out
    assert "✓ TSA certificate chain" in out and "PASSED" in out
    code, out = _zip_invoke(_bundle(tmp_path, good, b"tampered"), "--tsa-ca-bundle", bundle)
    assert code == 1 and "messageImprint does not match" in out
    code, out = _zip_invoke(_bundle(tmp_path, good, None), "--tsa-ca-bundle", bundle)
    assert code == 1 and "no attestations/run.intoto.json" in out
    code, out = _zip_invoke(_bundle(tmp_path, None), "--tsa-ca-bundle", bundle)
    assert code == 1 and "no RFC 3161 token" in out and "FAILED" in out
    code, out = _zip_invoke(_bundle(tmp_path, None))
    assert code == 0 and "TSA certificate chain" not in out
    code, out = _zip_invoke(_bundle(tmp_path, good, attestation))
    assert code == 0 and "TSA certificate chain" not in out
