"""An intact RFC 3161 token is not a trusted time — ``timestamp_trust`` says which it is.

Found by an adversarial review (2026-10-22): a real seal timestamped by the attacker's
*own* self-signed "TSA" with a 2001 ``genTime`` verified with plain ``nova verify
--json`` as exit 0, ``timestamp_ok: true``, ``timestamp_strict: true``, ``valid: true``
— and nothing in the JSON said the TSA was unanchored. ``check_timestamp`` (ADR-0297
S3) proves the token is intact and bound to the envelope; by design it does not say
*who* the TSA is (that is ``--tsa-ca-bundle`` / ``tsa_ca_certs``, ADR-0070).

``timestamp_ok`` keeps its ADR-0297/ADR-0301 meaning (token integrity) and exit code.
The additive ``timestamp_trust`` field — ``anchored`` / ``unanchored`` / ``absent`` /
``invalid`` — now states whether the *time* is trusted; human output prints
``UNANCHORED`` with the token's claimed genTime and TSA, ``--strict`` fails on it, and
the trust radar shows it as a warning rather than a passed axis. Tokens are built from
real keys and DER (``_tsa_pki``), not mocks.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.trust.novaseal import (
    TIMESTAMP_TRUST_ABSENT,
    TIMESTAMP_TRUST_ANCHORED,
    TIMESTAMP_TRUST_INVALID,
    TIMESTAMP_TRUST_UNANCHORED,
    KeyConfig,
    NovaSeal,
    timestamp_trust_for,
    unanchored_claims,
    verify_seal_dir,
    verify_with_profile,
)
from novafabric.trust.novaseal.config import SigningProfile

from ._tsa_pki import TokenSpec, TsaNode, make_tsa_cert, make_tsr
from ._x509_pki import Node, make_cert, make_pki

runner = CliRunner()
BACKDATED = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc)


@pytest.fixture(scope="module")
def real_root() -> Node:
    return make_cert("operator-tsa-root", issuer=None, ca=True)


@pytest.fixture(scope="module")
def real_tsa(real_root: Node) -> TsaNode:
    return make_tsa_cert(real_root, cn="operator-tsa")


@pytest.fixture(scope="module")
def evil_tsa() -> TsaNode:
    """The attacker's own root and TSA certificate — nothing anyone trusts."""
    return make_tsa_cert(make_cert("attacker-root", issuer=None, ca=True), cn="totally-a-real-tsa")


@pytest.fixture()
def sealed(tmp_path: Path) -> tuple[Path, Path, bytes]:
    """(capsule_dir, novaseal.yaml, dsse_bytes) — a real seal, no TSA at seal time."""
    pki = make_pki("ts-trust")
    key_path, cert_path = tmp_path / "seal.key", tmp_path / "seal.crt"
    key_path.write_bytes(pki.leaf.key_pem)
    cert_path.write_bytes(pki.leaf.cert_pem)
    merkle_db = tmp_path / "merkle.db"
    config = tmp_path / "novaseal.yaml"
    config.write_text(
        f"profile: local\nkey_path: {key_path}\ncert_path: {cert_path}\n"
        f"tsa_url: \nmerkle_db: {merkle_db}\n"
    )
    seal = NovaSeal(
        config=KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path)),
        tsa_url="",
        db_path=str(merkle_db),
    )
    bundle = seal.seal({"run_id": "ts-trust-001", "status": "success"})
    seal._merkle.close()
    seal_dir = tmp_path / "capsule" / ".seal"
    seal_dir.mkdir(parents=True)
    (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal_dir / "log-entry.json").write_text(json.dumps(bundle.log_entry), encoding="utf-8")
    return seal_dir.parent, config, bundle.dsse_envelope


def _timestamp(capsule: Path, dsse: bytes, tsa: TsaNode, when: datetime.datetime | None = None):  # type: ignore[no-untyped-def]
    spec = TokenSpec(digest=hashlib.sha256(dsse).digest())
    if when is not None:
        spec = TokenSpec(digest=hashlib.sha256(dsse).digest(), gen_time=when)
    (capsule / ".seal" / "manifest.dsse.tsr").write_bytes(make_tsr(tsa, spec))


def _json(args: list[str]):  # type: ignore[no-untyped-def]
    result = runner.invoke(app, ["verify", *args, "--json"])
    return result, json.loads(result.stdout)


# ---------------------------------------------------------------------------
# The attack: a self-made, backdated TSA
# ---------------------------------------------------------------------------


def test_selfmade_backdated_token_is_intact_but_unanchored(
    sealed: tuple[Path, Path, bytes], evil_tsa: TsaNode
) -> None:
    capsule, _config, dsse = sealed
    _timestamp(capsule, dsse, evil_tsa, BACKDATED)
    result = verify_seal_dir(capsule / ".seal")
    assert result.timestamp_ok is True  # integrity: the token is genuinely bound
    assert result.timestamp_trust == TIMESTAMP_TRUST_UNANCHORED
    assert result.timestamp_gen_time == "2001-01-01T00:00:00+00:00"
    assert "totally-a-real-tsa" in (result.tsa_subject or "")
    assert "timestamp-unanchored" in unanchored_claims(result)


def test_nova_verify_json_says_the_selfmade_tsa_is_unanchored(
    sealed: tuple[Path, Path, bytes], evil_tsa: TsaNode
) -> None:
    capsule, config, dsse = sealed
    _timestamp(capsule, dsse, evil_tsa, BACKDATED)
    result, report = _json([str(capsule), "--seal-config", str(config)])
    # Exit contract unchanged (ADR-0297 S3 / ADR-0301): integrity passed.
    assert result.exit_code == 0, result.output
    assert report["timestamp_ok"] is True
    assert report["timestamp_trust"] == TIMESTAMP_TRUST_UNANCHORED
    assert report["timestamp_gen_time"].startswith("2001-01-01")
    assert "timestamp-unanchored" in report["unanchored_claims"]


def test_human_output_does_not_print_ok_for_an_unanchored_time(
    sealed: tuple[Path, Path, bytes], evil_tsa: TsaNode
) -> None:
    capsule, config, dsse = sealed
    _timestamp(capsule, dsse, evil_tsa, BACKDATED)
    result = runner.invoke(app, ["verify", str(capsule), "--seal-config", str(config)])
    assert result.exit_code == 0, result.output
    assert "Timestamp (RFC 3161): UNANCHORED" in result.output
    assert "Timestamp (RFC 3161): OK" not in result.output
    assert "2001-01-01" in result.output
    assert "timestamp_trust=unanchored" in result.output


def test_strict_fails_an_unanchored_timestamp(
    sealed: tuple[Path, Path, bytes], evil_tsa: TsaNode
) -> None:
    capsule, config, dsse = sealed
    _timestamp(capsule, dsse, evil_tsa, BACKDATED)
    result, report = _json([str(capsule), "--seal-config", str(config), "--strict"])
    assert result.exit_code == 1, result.output
    assert report["valid"] is True  # the seal verified; --strict asked for more
    assert "timestamp-unanchored" in report["unanchored_claims"]


def test_selfmade_tsa_against_the_operators_anchor_is_invalid(
    sealed: tuple[Path, Path, bytes], evil_tsa: TsaNode, real_root: Node, tmp_path: Path
) -> None:
    capsule, config, dsse = sealed
    _timestamp(capsule, dsse, evil_tsa, BACKDATED)
    anchor = tmp_path / "tsa-ca.pem"
    anchor.write_bytes(real_root.cert_pem)
    result, report = _json(
        [str(capsule), "--seal-config", str(config), "--tsa-ca-bundle", str(anchor)]
    )
    assert result.exit_code == 1, result.output
    assert report["timestamp_trust"] == TIMESTAMP_TRUST_INVALID


# ---------------------------------------------------------------------------
# Legitimate cases keep working
# ---------------------------------------------------------------------------


def test_anchored_token_is_trusted_time(
    sealed: tuple[Path, Path, bytes], real_tsa: TsaNode, real_root: Node, tmp_path: Path
) -> None:
    capsule, config, dsse = sealed
    _timestamp(capsule, dsse, real_tsa)
    anchor = tmp_path / "tsa-ca.pem"
    anchor.write_bytes(real_root.cert_pem)
    result, report = _json(
        [str(capsule), "--seal-config", str(config), "--tsa-ca-bundle", str(anchor)]
    )
    assert result.exit_code == 0, result.output
    assert report["timestamp_ok"] is True
    assert report["timestamp_trust"] == TIMESTAMP_TRUST_ANCHORED
    assert "timestamp-unanchored" not in report["unanchored_claims"]
    human = runner.invoke(
        app,
        ["verify", str(capsule), "--seal-config", str(config), "--tsa-ca-bundle", str(anchor)],
    )
    assert "Timestamp (RFC 3161): OK" in human.output


def test_no_token_is_absent_and_claims_no_time(sealed: tuple[Path, Path, bytes]) -> None:
    capsule, config, _dsse = sealed
    result, report = _json([str(capsule), "--seal-config", str(config)])
    assert result.exit_code == 0, result.output
    assert report["timestamp_ok"] is None
    assert report["timestamp_trust"] == TIMESTAMP_TRUST_ABSENT
    assert "timestamp-unanchored" not in report["unanchored_claims"]


# ---------------------------------------------------------------------------
# Library: verify_seal_dir(tsa_anchors_pem=...) and verify_with_profile
# ---------------------------------------------------------------------------


def test_library_anchor_check(
    sealed: tuple[Path, Path, bytes], real_tsa: TsaNode, evil_tsa: TsaNode, real_root: Node
) -> None:
    capsule, _config, dsse = sealed
    seal_dir = capsule / ".seal"
    anchors = real_root.cert_pem

    no_token = verify_seal_dir(seal_dir, tsa_anchors_pem=anchors)
    assert no_token.valid is False  # anchors configured: a missing token fails
    assert no_token.timestamp_trust == TIMESTAMP_TRUST_ABSENT

    _timestamp(capsule, dsse, real_tsa)
    good = verify_seal_dir(seal_dir, tsa_anchors_pem=anchors)
    assert good.valid, good.errors
    assert good.timestamp_trust == TIMESTAMP_TRUST_ANCHORED

    _timestamp(capsule, dsse, evil_tsa, BACKDATED)
    bad = verify_seal_dir(seal_dir, tsa_anchors_pem=anchors)
    assert bad.valid is False
    assert bad.timestamp_trust == TIMESTAMP_TRUST_INVALID
    assert any("TSA trust chain failed" in e for e in bad.errors), bad.errors


def test_verify_with_profile_anchors_with_tsa_ca_certs(
    sealed: tuple[Path, Path, bytes], real_tsa: TsaNode, real_root: Node, tmp_path: Path
) -> None:
    capsule, _config, dsse = sealed
    _timestamp(capsule, dsse, real_tsa)
    anchor = tmp_path / "tsa-ca.pem"
    anchor.write_bytes(real_root.cert_pem)
    unanchored = verify_with_profile(capsule / ".seal", SigningProfile(profile="local"))
    assert unanchored.timestamp_trust == TIMESTAMP_TRUST_UNANCHORED
    anchored = verify_with_profile(
        capsule / ".seal", SigningProfile(profile="local", tsa_ca_certs=[anchor])
    )
    assert anchored.timestamp_trust == TIMESTAMP_TRUST_ANCHORED


@pytest.mark.parametrize(
    ("timestamp_ok", "chain_ok", "expected"),
    [
        (None, None, TIMESTAMP_TRUST_ABSENT),
        (None, False, TIMESTAMP_TRUST_ABSENT),
        (False, None, TIMESTAMP_TRUST_INVALID),
        (True, False, TIMESTAMP_TRUST_INVALID),
        (True, None, TIMESTAMP_TRUST_UNANCHORED),
        (True, True, TIMESTAMP_TRUST_ANCHORED),
    ],
)
def test_timestamp_trust_for(
    timestamp_ok: bool | None, chain_ok: bool | None, expected: str
) -> None:
    assert timestamp_trust_for(timestamp_ok, chain_ok) == expected


# ---------------------------------------------------------------------------
# Consumers: the trust radar must not show an unanchored time as a passed axis
# ---------------------------------------------------------------------------


def test_radar_shows_an_unanchored_timestamp_as_a_warning() -> None:
    from novafabric.trust.capsule_flags import _timestamp_axis
    from novafabric.trust.radar import AxisState, RadarVerdict, build_trust_radar

    flags = {"signature_ok": True, "log_integrity_ok": True}
    unanchored = build_trust_radar({**flags, "timestamp_ok": _timestamp_axis("unanchored")})
    axis = next(a for a in unanchored.axes if a.key == "timestamp")
    assert axis.state is AxisState.warn
    assert unanchored.verdict is RadarVerdict.partial  # never "attested"

    anchored = build_trust_radar({**flags, "timestamp_ok": _timestamp_axis("anchored")})
    assert next(a for a in anchored.axes if a.key == "timestamp").state is AxisState.ok
    assert _timestamp_axis("absent") is None
    assert _timestamp_axis("invalid") is False


def test_evidence_bundle_verify_says_an_unanchored_token_is_not_checked(tmp_path: Path) -> None:
    """``nova verify <bundle.zip>`` never checked the token without --tsa-ca-bundle,
    and never said so either; now it says the time is not trusted."""
    import zipfile

    bundle = tmp_path / "b.zip"
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"artifacts": [], "bundle_id": "b"}))
        zf.writestr("manifest.dsse.tsr", b"\x30\x03\x02\x01\x00")
    result = runner.invoke(app, ["verify", str(bundle)])
    assert "Timestamp (RFC 3161): NOT CHECKED" in result.output


# ---------------------------------------------------------------------------
# Consumers: `nova serve` endpoints
# ---------------------------------------------------------------------------


def test_serve_capsule_verify_reports_timestamp_trust(
    sealed: tuple[Path, Path, bytes],
    evil_tsa: TsaNode,
    real_tsa: TsaNode,
    real_root: Node,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from novafabric.serve.app import create_app

    capsule, config, dsse = sealed
    (capsule / "capsule.yaml").write_text("run_id: ts-trust-001\n")
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(config))
    token = "test-token-1234567890abcdef"
    headers = {"host": "127.0.0.1:4321", "Authorization": f"Bearer {token}"}
    app = create_app(
        token=token, capsule_dir=capsule.parent, db_path=tmp_path / "r.db", static_dir=None
    )
    _timestamp(capsule, dsse, evil_tsa, BACKDATED)
    with TestClient(app) as client:
        body = client.post(f"/api/runs/{capsule.name}/verify", headers=headers).json()
    assert body["timestamp_ok"] is True
    assert body["timestamp_trust"] == TIMESTAMP_TRUST_UNANCHORED
    assert "timestamp-unanchored" in body["unanchored_claims"]

    # tsa_ca_certs in novaseal.yaml anchors it — and refuses the self-made TSA.
    anchor = tmp_path / "tsa-ca.pem"
    anchor.write_bytes(real_root.cert_pem)
    config.write_text(config.read_text() + f"tsa_ca_certs:\n  - {anchor}\n")
    with TestClient(app) as client:
        refused = client.post(f"/api/runs/{capsule.name}/verify", headers=headers).json()
        _timestamp(capsule, dsse, real_tsa)
        anchored = client.post(f"/api/runs/{capsule.name}/verify", headers=headers).json()
    assert refused["timestamp_trust"] == TIMESTAMP_TRUST_INVALID
    assert refused["valid"] is False
    assert anchored["timestamp_trust"] == TIMESTAMP_TRUST_ANCHORED
    assert anchored["valid"] is True, anchored["errors"]


def test_serve_bundle_tsa_chain_check(
    real_tsa: TsaNode,
    evil_tsa: TsaNode,
    real_root: Node,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastapi")
    from novafabric.serve.app import _bundle_tsa_chain_ok

    statement = b'{"_type":"https://in-toto.io/Statement/v1"}'
    spec = TokenSpec(digest=hashlib.sha256(statement).digest())
    good, evil = make_tsr(real_tsa, spec), make_tsr(evil_tsa, spec)
    monkeypatch.delenv("NOVAFABRIC_SEAL_CONFIG", raising=False)
    assert _bundle_tsa_chain_ok(good, statement) is None  # no anchors → unanchored

    pki = make_pki("bundle-ts")
    key, cert = tmp_path / "k.pem", tmp_path / "c.pem"
    key.write_bytes(pki.leaf.key_pem)
    cert.write_bytes(pki.leaf.cert_pem)
    anchor = tmp_path / "tsa-ca.pem"
    anchor.write_bytes(real_root.cert_pem)
    config = tmp_path / "novaseal.yaml"
    config.write_text(
        f"profile: local\nkey_path: {key}\ncert_path: {cert}\ntsa_url: \n"
        f"merkle_db: {tmp_path / 'm.db'}\ntsa_ca_certs:\n  - {anchor}\n"
    )
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(config))
    assert _bundle_tsa_chain_ok(good, statement) is True
    assert _bundle_tsa_chain_ok(evil, statement) is False
