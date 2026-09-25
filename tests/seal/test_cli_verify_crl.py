"""``nova verify --ca-bundle … --crl-dir DIR [--crl-strict]`` — ADR-0070 §3 (experimental).

Acceptance criteria:

* Without ``--crl-dir`` (and no ``crl_dir`` in novaseal.yaml) the CA-bundle output is
  unchanged — no revocation lines.
* Fresh CRLs for the whole path → exit 0 with a visible ``GOOD`` line per certificate.
* A revoked leaf or intermediate → exit 1 (soft mode too), reason shown.
* A missing CRL → exit 0 with a visible ``WARNING no_crl`` line; ``--crl-strict`` → exit 1.
* A forged CRL (wrong key) never revokes; it is a warning (soft) / failure (strict).
* Non-CRL files are reported as ``skipped``; an over-limit directory fails closed.
* ``--crl-dir`` without any CA bundle is a usage error (exit 2).
* ``crl_dir`` / ``crl_strict`` from novaseal.yaml are honoured; bad values are config errors.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.trust.novaseal import KeyConfig, NovaSeal
from novafabric.trust.novaseal.crl import DEFAULT_MAX_CRL_FILES

from ._x509_pki import NOW, Pki, Revoked, crl_der, crl_pem, make_crl, make_pki

runner = CliRunner()
DAY = datetime.timedelta(days=1)


@pytest.fixture()
def sealed(tmp_path: Path) -> tuple[Path, Path, Path, Pki]:
    """(capsule_dir, config, bundle, pki): a capsule sealed by a CA-issued leaf."""
    pki = make_pki("clicrl")
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
    bundle = seal.seal({"run_id": "crl-001", "status": "success"})
    capsule_dir = tmp_path / "crl-capsule"
    seal_dir = capsule_dir / ".seal"
    seal_dir.mkdir(parents=True)
    (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal_dir / "manifest.dsse.tsr").write_bytes(bundle.tsr)
    (seal_dir / "log-entry.json").write_text(json.dumps(bundle.log_entry), encoding="utf-8")
    # The envelope carries only the leaf, so the bundle must carry the intermediate.
    ca_bundle = tmp_path / "bundle.pem"
    ca_bundle.write_bytes(pki.root.cert_pem + pki.intermediate.cert_pem)
    return capsule_dir, config_path, ca_bundle, pki


def _crl_dir(tmp_path: Path, files: dict[str, bytes]) -> Path:
    d = tmp_path / "crls"
    d.mkdir(exist_ok=True)
    for name, data in files.items():
        (d / name).write_bytes(data)
    return d


def _invoke(capsule_dir: Path, config: Path, *extra: str) -> tuple[int, str]:
    result = runner.invoke(app, ["verify", str(capsule_dir), "--seal-config", str(config), *extra])
    return result.exit_code, " ".join(result.output.split())


def test_no_crl_dir_leaves_output_unchanged(sealed: tuple[Path, Path, Path, Pki]) -> None:
    capsule_dir, config, bundle, _ = sealed
    code, out = _invoke(capsule_dir, config, "--ca-bundle", str(bundle))
    assert code == 0, out
    assert "Revocation (CRL" not in out


def test_fresh_crl_passes_with_good_line(
    tmp_path: Path, sealed: tuple[Path, Path, Path, Pki]
) -> None:
    capsule_dir, config, bundle, pki = sealed
    crls = _crl_dir(tmp_path, {"inter.crl": crl_der(make_crl(pki.intermediate))})
    code, out = _invoke(capsule_dir, config, "--ca-bundle", str(bundle), "--crl-dir", str(crls))
    assert code == 0, out
    assert "Revocation (CRL, offline, soft-fail)" in out
    assert "GOOD CN=clicrl-signer" in out


def test_revoked_leaf_fails(tmp_path: Path, sealed: tuple[Path, Path, Path, Pki]) -> None:
    capsule_dir, config, bundle, pki = sealed
    from cryptography import x509

    crl = make_crl(
        pki.intermediate,
        (Revoked(pki.leaf.cert.serial_number, NOW - DAY, x509.ReasonFlags.key_compromise),),
    )
    crls = _crl_dir(tmp_path, {"inter.pem": crl_pem(crl)})
    code, out = _invoke(capsule_dir, config, "--ca-bundle", str(bundle), "--crl-dir", str(crls))
    assert code == 1, out
    assert "REVOKED" in out and "key_compromise" in out


def test_revoked_intermediate_fails(tmp_path: Path, sealed: tuple[Path, Path, Path, Pki]) -> None:
    """The bundled intermediate terminates the path, yet the root's CRL still revokes it."""
    capsule_dir, config, bundle, pki = sealed
    crls = _crl_dir(
        tmp_path,
        {
            "inter.crl": crl_der(make_crl(pki.intermediate)),
            "root.crl": crl_der(
                make_crl(pki.root, (Revoked(pki.intermediate.cert.serial_number, NOW - DAY),))
            ),
        },
    )
    code, out = _invoke(capsule_dir, config, "--ca-bundle", str(bundle), "--crl-dir", str(crls))
    assert code == 1, out
    assert "REVOKED CN=clicrl-intermediate-ca" in out


def test_missing_crl_warns_then_strict_fails(
    tmp_path: Path, sealed: tuple[Path, Path, Path, Pki]
) -> None:
    capsule_dir, config, bundle, _ = sealed
    crls = _crl_dir(tmp_path, {"notes.txt": b"hello"})
    code, out = _invoke(capsule_dir, config, "--ca-bundle", str(bundle), "--crl-dir", str(crls))
    assert code == 0, out
    assert "WARNING no_crl" in out
    assert "skipped notes.txt" in out
    code, out = _invoke(
        capsule_dir, config, "--ca-bundle", str(bundle), "--crl-dir", str(crls), "--crl-strict"
    )
    assert code == 1, out
    assert "FAIL no_crl" in out
    assert "strict" in out


def test_forged_crl_never_revokes(tmp_path: Path, sealed: tuple[Path, Path, Path, Pki]) -> None:
    capsule_dir, config, bundle, pki = sealed
    forged = make_crl(
        pki.intermediate,
        (Revoked(pki.leaf.cert.serial_number, NOW - DAY),),
        signer_key=ec.generate_private_key(ec.SECP256R1()),
    )
    crls = _crl_dir(tmp_path, {"forged.crl": crl_der(forged)})
    code, out = _invoke(capsule_dir, config, "--ca-bundle", str(bundle), "--crl-dir", str(crls))
    assert code == 0, out
    assert "WARNING invalid_crl" in out
    code, _ = _invoke(
        capsule_dir, config, "--ca-bundle", str(bundle), "--crl-dir", str(crls), "--crl-strict"
    )
    assert code == 1


def test_stale_crl_strict_fails(tmp_path: Path, sealed: tuple[Path, Path, Path, Pki]) -> None:
    capsule_dir, config, bundle, pki = sealed
    stale = make_crl(pki.intermediate, this_update=NOW - 9 * DAY, next_update=NOW - DAY)
    crls = _crl_dir(tmp_path, {"stale.crl": crl_der(stale)})
    code, out = _invoke(capsule_dir, config, "--ca-bundle", str(bundle), "--crl-dir", str(crls))
    assert code == 0 and "WARNING stale" in out
    code, out = _invoke(
        capsule_dir, config, "--ca-bundle", str(bundle), "--crl-dir", str(crls), "--crl-strict"
    )
    assert code == 1 and "FAIL stale" in out


def test_over_limit_directory_fails_closed(
    tmp_path: Path, sealed: tuple[Path, Path, Path, Pki]
) -> None:
    capsule_dir, config, bundle, _ = sealed
    crls = _crl_dir(tmp_path, {f"{i:04d}.crl": b"x" for i in range(DEFAULT_MAX_CRL_FILES + 1)})
    code, out = _invoke(capsule_dir, config, "--ca-bundle", str(bundle), "--crl-dir", str(crls))
    assert code == 1
    assert "over the limit" in out


def test_crl_dir_without_bundle_is_usage_error(
    tmp_path: Path, sealed: tuple[Path, Path, Path, Pki]
) -> None:
    capsule_dir, config, _, _ = sealed
    crls = _crl_dir(tmp_path, {})
    result = runner.invoke(
        app, ["verify", str(capsule_dir), "--seal-config", str(config), "--crl-dir", str(crls)]
    )
    assert result.exit_code == 2


def test_config_crl_dir_and_strict_are_honoured(
    tmp_path: Path, sealed: tuple[Path, Path, Path, Pki]
) -> None:
    capsule_dir, config, bundle, _ = sealed
    crls = _crl_dir(tmp_path, {})
    base = config.read_text()
    config.write_text(base + f"ca_bundle: {bundle}\ncrl_dir: {crls}\n")
    code, out = _invoke(capsule_dir, config)
    assert code == 0 and "WARNING no_crl" in out
    config.write_text(base + f"ca_bundle: {bundle}\ncrl_dir: {crls}\ncrl_strict: true\n")
    code, out = _invoke(capsule_dir, config)
    assert code == 1 and "FAIL no_crl" in out


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("crl_dir: /nonexistent/novaseal-crl\n", "crl_dir not found"),
        ("crl_dir: ''\n", "crl_dir must be a non-empty path"),
        ("crl_strict: maybe\n", "crl_strict must be true or false"),
    ],
)
def test_bad_crl_config_is_a_config_error(
    sealed: tuple[Path, Path, Path, Pki], line: str, message: str
) -> None:
    capsule_dir, config, _, _ = sealed
    config.write_text(config.read_text() + line)
    code, out = _invoke(capsule_dir, config)
    assert code == 1
    assert message in out


def test_config_crl_without_ca_bundle_is_a_config_error(
    tmp_path: Path, sealed: tuple[Path, Path, Path, Pki]
) -> None:
    # crl_dir / crl_strict without ca_bundle would check nothing; refuse loudly.
    capsule_dir, config, _, _ = sealed
    crls = _crl_dir(tmp_path, {})
    base = config.read_text()
    for extra in (f"crl_dir: {crls}\n", "crl_strict: true\n"):
        config.write_text(base + extra)
        code, out = _invoke(capsule_dir, config)
        assert code == 1
        assert "require ca_bundle" in out
