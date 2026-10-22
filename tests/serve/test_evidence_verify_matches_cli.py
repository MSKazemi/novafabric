"""``POST /api/evidence/{bundle_id}/verify`` reaches the same verdict as ``nova verify``.

M11: the route checked only the run attestation's DSSE signature under the
certificate shipped *inside* the bundle (plus the RFC 3161 token). It recomputed no
artifact digest, no ``manifest_hash`` and no signed subject, so the dashboard showed a
tampered or re-signed bundle as ``valid: true`` while ``nova verify`` failed it.

Every case below is built from one real exported bundle and attacks it the way the
format allows: the signing key ships in the bundle, so the attacker repairs every
digest and ``manifest_hash`` and may re-sign with a key of their own. Each case asserts
the serve verdict, then asserts ``nova verify`` (with ``--public-key`` when the server
has a trust anchor) agrees with it.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import sys
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.cli.main import app as cli_app
from novafabric.evidence.bundle import PREDICATE_RUN, _canonical_manifest_hash
from novafabric.evidence.intoto import DSSE_PAYLOAD_TYPE, dsse_sign_payload
from novafabric.evidence.merkle import ROOT_VERSIONS, root_over_files
from novafabric.evidence.signing import LocalSigner, generate_keypair
from novafabric.serve import evidence_verify
from novafabric.serve.app import create_app

TOKEN = "test-token"
HOST = {"host": "127.0.0.1:4321"}
RUN_ATT = "attestations/run.intoto.json"
LINEAGE_ATT = "attestations/lineage.intoto.json"
CAPSULE = "run-capsule/"
runner = CliRunner()

Files = dict[str, bytes]
Manifest = dict[str, Any]


@dataclass(frozen=True)
class Exported:
    public_key: Path
    files: Files
    manifest: Manifest

    def copy(self) -> tuple[Files, Manifest]:
        return dict(self.files), copy.deepcopy(self.manifest)


@pytest.fixture(scope="module")
def exported(tmp_path_factory: pytest.TempPathFactory) -> Exported:
    tmp = tmp_path_factory.mktemp("m11")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("NOVAFABRIC_HOME", str(tmp / "home"))
        mp.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
        capsule = CaptureOrchestrator(base_dir=tmp / "runs").run(
            command=[sys.executable, "-c", "pass"]
        ).capsule_dir
        priv, pub = generate_keypair(tmp / "keys")
        out = tmp / "ev.zip"
        result = runner.invoke(
            cli_app, ["export-evidence", str(capsule), "--key", str(priv), "--output", str(out)]
        )
    assert result.exit_code == 0, result.output
    with zipfile.ZipFile(out) as zf:
        files = {n: zf.read(n) for n in zf.namelist()}
    return Exported(pub, files, json.loads(files["manifest.json"]))


@pytest.fixture(scope="module")
def attacker(tmp_path_factory: pytest.TempPathFactory) -> LocalSigner:
    priv, _ = generate_keypair(tmp_path_factory.mktemp("attacker"))
    return LocalSigner(priv)


@pytest.fixture()
def evidence_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "evidence"
    d.mkdir()
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_DIR", str(d))
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(tmp_path / "no-novaseal.yaml"))
    return d


@pytest.fixture()
def client(tmp_path: Path, evidence_dir: Path) -> TestClient:
    return TestClient(create_app(token=TOKEN, capsule_dir=tmp_path, db_path=None))


# ── attacker helpers ───────────────────────────────────────────────────────────


def _statement(files: Files, path: str) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(base64.b64decode(json.loads(files[path])["payload"]))
    return out


def _sign_into(files: Files, manifest: Manifest, path: str, stmt: Any, signer: LocalSigner) -> None:
    env = dsse_sign_payload(json.dumps(stmt).encode(), DSSE_PAYLOAD_TYPE, signer)
    entry = next(s for s in manifest["signatures"] if s["envelope_path"] == path)
    files[path] = json.dumps(env).encode()
    files[entry["signature_path"]] = base64.b64decode(env["signatures"][0]["sig"])
    files[entry["certificate_path"]] = signer.public_pem


def _repair(files: Files, manifest: Manifest) -> None:
    """Write every member's true digest back and recompute the self-hash."""
    for art in manifest["artifacts"]:
        if art["path"] in files:
            art["sha256"] = "sha256:" + hashlib.sha256(files[art["path"]]).hexdigest()
    manifest["manifest_hash"] = _canonical_manifest_hash(manifest)


def _capsule_root(files: Files, version: int) -> str:
    members = [(n[len(CAPSULE):], d) for n, d in files.items() if n.startswith(CAPSULE)]
    return root_over_files(members, version=version).removeprefix("sha256:")


def _a_capsule_member(files: Files) -> str:
    return sorted(n for n in files if n.startswith(CAPSULE) and n.endswith("capsule.yaml"))[0]


def tamper_artifact_byte(files: Files, manifest: Manifest, _a: LocalSigner) -> None:
    """(a) one byte of a capsule file changes; digests are repaired."""
    name = _a_capsule_member(files)
    data = bytearray(files[name])
    data[len(data) // 2] ^= 0x01
    files[name] = bytes(data)
    _repair(files, manifest)


def tamper_artifact_byte_unrepaired(files: Files, _m: Manifest, _a: LocalSigner) -> None:
    """(a') the naive tamper: the manifest still lists the old digest."""
    name = _a_capsule_member(files)
    files[name] = files[name] + b"\n# edited\n"


def edit_manifest_and_rehash(files: Files, manifest: Manifest, _a: LocalSigner) -> None:
    """(b) the manifest's subject is retargeted and its self-hash recomputed."""
    manifest["subject"]["capsule_hash"] = "sha256:" + "ab" * 32
    _repair(files, manifest)


def resign_tampered_capsule(files: Files, manifest: Manifest, attacker: LocalSigner) -> None:
    """(c) the capsule is edited and every attestation re-signed by the attacker.

    Everything is made consistent: the new capsule root is signed as the run subject,
    the manifest subject and ``subject_hashes`` follow, and the attacker's key is the
    certificate in the bundle. Only a trust anchor can tell this from the original.
    """
    old = _statement(files, RUN_ATT)["subject"][0]["digest"]["sha256"]
    version = next(v for v in ROOT_VERSIONS if _capsule_root(files, v) == old.removeprefix("sha256:"))
    name = _a_capsule_member(files)
    files[name] = files[name] + b"# attacker was here\n"
    root = _capsule_root(files, version)
    for att in manifest["attestations"]:
        path = att["path"]
        stmt = _statement(files, path)
        if stmt["predicateType"] == PREDICATE_RUN:
            stmt["subject"][0]["digest"]["sha256"] = root
            att["subject_hashes"] = ["sha256:" + root]
            manifest["subject"]["capsule_hash"] = "sha256:" + root
        _sign_into(files, manifest, path, stmt, attacker)
    _repair(files, manifest)


def swap_subjects(files: Files, manifest: Manifest, _a: LocalSigner) -> None:
    """(d) the run and lineage attestations' subject hashes are swapped in the manifest."""
    by_path = {a["path"]: a for a in manifest["attestations"]}
    run, lineage = by_path[RUN_ATT], by_path[LINEAGE_ATT]
    run["subject_hashes"], lineage["subject_hashes"] = (
        lineage["subject_hashes"],
        run["subject_hashes"],
    )
    _repair(files, manifest)


def relabel_run_id(files: Files, manifest: Manifest, _a: LocalSigner) -> None:
    """(g) the unsigned manifest names another run; nothing signed changes."""
    manifest["subject"]["run_id"] = "someone-elses-run"
    _repair(files, manifest)


def genuine(_f: Files, _m: Manifest, _a: LocalSigner) -> None:
    """(e)/(f) the bundle as exported."""


Attack = Callable[[Files, Manifest, LocalSigner], None]


def _install(
    exported: Exported, attacker: LocalSigner, attack: Attack, evidence_dir: Path, bundle_id: str
) -> Path:
    files, manifest = exported.copy()
    attack(files, manifest, attacker)
    files["manifest.json"] = json.dumps(manifest, indent=2).encode()
    dst = evidence_dir / f"{bundle_id}.zip"
    with zipfile.ZipFile(dst, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return dst


def _serve(client: TestClient, bundle_id: str) -> dict[str, Any]:
    r = client.post(f"/api/evidence/{bundle_id}/verify", params={"token": TOKEN}, headers=HOST)
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def _cli_passes(path: Path, public_key: Path | None) -> bool:
    extra = ["--public-key", str(public_key)] if public_key is not None else []
    result = runner.invoke(cli_app, ["verify", *extra, str(path)])
    assert result.exit_code in (0, 1), result.output
    if result.exit_code == 1:
        assert isinstance(result.exception, SystemExit), repr(result.exception)  # not a crash
    return result.exit_code == 0


# ── the attack matrix ──────────────────────────────────────────────────────────

# (attack, anchored, expected valid, expected signer_trust, a field that must be false)
CASES = [
    pytest.param(tamper_artifact_byte, False, False, "unanchored", "subjects_ok", id="a-artifact-byte-repaired"),
    pytest.param(tamper_artifact_byte_unrepaired, False, False, "unanchored", "artifacts_ok", id="a-artifact-byte"),
    pytest.param(edit_manifest_and_rehash, False, False, "unanchored", "subjects_ok", id="b-manifest-rehashed"),
    pytest.param(resign_tampered_capsule, False, True, "unanchored", None, id="c-attacker-resigned-unanchored"),
    pytest.param(resign_tampered_capsule, True, False, "untrusted", None, id="c-attacker-resigned-anchored"),
    pytest.param(swap_subjects, False, False, "unanchored", "subjects_ok", id="d-subjects-swapped"),
    pytest.param(relabel_run_id, True, False, "anchored", "subjects_ok", id="g-run-id-relabelled-anchored"),
    pytest.param(genuine, False, True, "unanchored", None, id="e-genuine-unanchored"),
    pytest.param(genuine, True, True, "anchored", None, id="f-genuine-anchored"),
]


@pytest.mark.parametrize(("attack", "anchored", "valid", "trust", "false_field"), CASES)
def test_serve_verdict_and_cli_agreement(
    exported: Exported,
    attacker: LocalSigner,
    evidence_dir: Path,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    attack: Attack,
    anchored: bool,
    valid: bool,
    trust: str,
    false_field: str | None,
) -> None:
    if anchored:
        monkeypatch.setenv("NOVAFABRIC_EVIDENCE_TRUSTED_KEYS", str(exported.public_key))
    path = _install(exported, attacker, attack, evidence_dir, "bundle-under-test")

    body = _serve(client, "bundle-under-test")

    assert body["valid"] is valid, body["errors"]
    assert body["signer_trust"] == trust
    assert bool(body["errors"]) is (not valid)
    if false_field is not None:
        assert body[false_field] is False, body
    if trust == "unanchored":
        # never presented as authenticated: the verifying key came from the bundle
        assert body["signer_fingerprints"], body
    assert _cli_passes(path, exported.public_key if anchored else None) is body["valid"]


def test_the_attacker_resigned_bundle_names_the_attackers_key(
    exported: Exported, attacker: LocalSigner, evidence_dir: Path, client: TestClient
) -> None:
    """Unanchored, a consistent forgery verifies; the response must show whose key it was."""
    _install(exported, attacker, resign_tampered_capsule, evidence_dir, "forged")
    body = _serve(client, "forged")
    attacker_fp = "sha256:" + hashlib.sha256(
        serialization.load_pem_public_key(attacker.public_pem).public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    ).hexdigest()
    assert body["signer_trust"] == "unanchored"
    assert body["signer_fingerprints"] == [attacker_fp]


def test_the_reported_run_id_is_the_signed_one_not_the_file_name(
    exported: Exported, attacker: LocalSigner, evidence_dir: Path, client: TestClient
) -> None:
    """The local seal check targets the run the signature names, not ``<bundle_id>.zip``."""
    _install(exported, attacker, genuine, evidence_dir, "renamed-file")
    signed = _statement(exported.files, RUN_ATT)["predicate"]["run_id"]
    assert _serve(client, "renamed-file")["run_id"] == signed != "renamed-file"


def test_a_key_in_the_request_is_never_an_anchor(
    exported: Exported, attacker: LocalSigner, evidence_dir: Path, client: TestClient
) -> None:
    _install(exported, attacker, genuine, evidence_dir, "b1")
    r = client.post(
        "/api/evidence/b1/verify",
        params={"token": TOKEN, "public_key": exported.public_key.read_text()},
        headers=HOST,
        json={"public_key": exported.public_key.read_text(), "trusted_keys": ["x"]},
    )
    assert r.status_code == 200
    assert r.json()["signer_trust"] == "unanchored"


def test_a_trust_list_with_several_keys_anchors_any_of_them(
    exported: Exported,
    attacker: LocalSigner,
    evidence_dir: Path,
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trust = tmp_path / "trusted.pem"
    trust.write_bytes(attacker.public_pem + b"\n" + exported.public_key.read_bytes())
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_TRUSTED_KEYS", str(trust))
    _install(exported, attacker, genuine, evidence_dir, "b2")
    body = _serve(client, "b2")
    assert (body["valid"], body["signer_trust"]) == (True, "anchored")


def test_the_default_trust_list_under_nova_home_is_read(
    exported: Exported, attacker: LocalSigner, evidence_dir: Path, client: TestClient
) -> None:
    home = Path(os.environ["NOVAFABRIC_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "evidence-trusted-keys.pem").write_bytes(attacker.public_pem)
    _install(exported, attacker, genuine, evidence_dir, "b3")
    body = _serve(client, "b3")
    assert (body["valid"], body["signer_trust"]) == (False, "untrusted")


def _ec_public_pem() -> bytes:
    return (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )


@pytest.mark.parametrize(
    "content",
    [None, b"", b"not a pem", b"-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n", "ec"],
    ids=["missing", "empty", "no-pem", "bad-key", "not-ed25519"],
)
def test_an_unusable_configured_anchor_fails_closed(
    exported: Exported,
    attacker: LocalSigner,
    evidence_dir: Path,
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: bytes | str | None,
) -> None:
    """A broken trust list is an error, never a silent fall-back to "unanchored"."""
    trust = tmp_path / "trusted.pem"
    if content == "ec":
        trust.write_bytes(_ec_public_pem())
    elif isinstance(content, bytes):
        trust.write_bytes(content)
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_TRUSTED_KEYS", str(trust))
    _install(exported, attacker, genuine, evidence_dir, "b4")
    r = client.post("/api/evidence/b4/verify", params={"token": TOKEN}, headers=HOST)
    assert r.status_code == 500
    assert "trust list" in r.json()["detail"]


def test_a_fifo_trust_list_never_hangs(
    exported: Exported,
    attacker: LocalSigner,
    evidence_dir: Path,
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fifo = tmp_path / "trusted.fifo"
    os.mkfifo(fifo)
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_TRUSTED_KEYS", str(fifo))
    _install(exported, attacker, genuine, evidence_dir, "b5")
    r = client.post("/api/evidence/b5/verify", params={"token": TOKEN}, headers=HOST)
    assert r.status_code == 500


def test_a_symlinked_bundle_is_refused(
    exported: Exported, attacker: LocalSigner, evidence_dir: Path, client: TestClient, tmp_path: Path
) -> None:
    real = _install(exported, attacker, genuine, tmp_path, "elsewhere")
    (evidence_dir / "linked.zip").symlink_to(real)
    r = client.post("/api/evidence/linked/verify", params={"token": TOKEN}, headers=HOST)
    assert r.status_code == 422


def test_an_oversized_bundle_is_refused_not_loaded(
    exported: Exported,
    attacker: LocalSigner,
    evidence_dir: Path,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(evidence_verify, "MAX_VERIFY_UNCOMPRESSED_BYTES", 1024)
    _install(exported, attacker, genuine, evidence_dir, "big")
    r = client.post("/api/evidence/big/verify", params={"token": TOKEN}, headers=HOST)
    assert r.status_code == 413
    assert "nova verify" in r.json()["detail"]


# ── the RFC 3161 token (checked by serve on top of the shared recipe) ──────────


def _zip_with(tmp_path: Path, members: dict[str, bytes]) -> zipfile.ZipFile:
    path = tmp_path / "t.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return zipfile.ZipFile(path)


def _no_anchor(_token: bytes, _stamped: bytes) -> bool | None:
    return None


@pytest.mark.parametrize(
    ("members", "manifest", "expected"),
    [
        ({}, {}, (None, None)),
        ({}, {"timestamp_status": "failed"}, (False, None)),
        ({"manifest.dsse.tsr": b""}, {}, (None, None)),  # TSA skipped: absent, not passed
        ({"manifest.dsse.tsr": b"\x30\x03\x02\x01\x00"}, {}, (False, None)),  # no envelope
        ({"manifest.dsse.tsr": b"garbage", RUN_ATT: b"{}"}, {}, (False, None)),
    ],
    ids=["absent", "export-failed", "empty", "no-envelope", "garbage-token"],
)
def test_timestamp_outcomes(
    tmp_path: Path, members: Files, manifest: Manifest, expected: tuple[bool | None, bool | None]
) -> None:
    with _zip_with(tmp_path, members) as zf:
        ok, chain, errors = evidence_verify._check_timestamp(zf, manifest, _no_anchor)
    assert (ok, chain) == expected
    assert bool(errors) is (ok is False)


def test_an_oversized_token_is_refused_without_reading_it_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.trust.novaseal import tsa_token

    monkeypatch.setattr(tsa_token, "MAX_TOKEN_BYTES", 8)
    with _zip_with(tmp_path, {"manifest.dsse.tsr": b"x" * 64, RUN_ATT: b"{}"}) as zf:
        ok, _chain, errors = evidence_verify._check_timestamp(zf, {}, _no_anchor)
    assert ok is False and "larger than 8 bytes" in errors[0]


@pytest.mark.parametrize(
    ("chain", "expected"),
    [(None, (True, None)), (True, (True, True)), (False, (True, False))],
    ids=["unanchored", "anchored", "chain-fails"],
)
def test_an_intact_token_reports_the_anchor_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chain: bool | None,
    expected: tuple[bool, bool | None],
) -> None:
    from novafabric.trust.novaseal import timestamp

    monkeypatch.setattr(timestamp, "verify_timestamp", lambda _t, _e: True)
    with _zip_with(tmp_path, {"manifest.dsse.tsr": b"tok", RUN_ATT: b"{}"}) as zf:
        ok, got_chain, errors = evidence_verify._check_timestamp(zf, {}, lambda _t, _s: chain)
    assert (ok, got_chain) == expected
    assert bool(errors) is (chain is False)


def test_a_token_check_that_raises_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.trust.novaseal import timestamp

    def boom(_t: bytes, _e: bytes) -> bool:
        raise ValueError("hostile token")

    monkeypatch.setattr(timestamp, "verify_timestamp", boom)
    with _zip_with(tmp_path, {"manifest.dsse.tsr": b"tok", RUN_ATT: b"{}"}) as zf:
        ok, _chain, errors = evidence_verify._check_timestamp(zf, {}, _no_anchor)
    assert ok is False and "hostile token" in errors[0]


def test_a_duplicate_member_fails_both_verifiers(
    exported: Exported, attacker: LocalSigner, evidence_dir: Path, client: TestClient
) -> None:
    """zipfile reads the last entry of a name; another unzip may extract the first."""
    path = _install(exported, attacker, genuine, evidence_dir, "dup")
    name = _a_capsule_member(exported.files)
    with pytest.warns(UserWarning, match="Duplicate name"), zipfile.ZipFile(path, "a") as zf:
        zf.writestr(name, exported.files[name])
    body = _serve(client, "dup")
    assert body["valid"] is False and body["no_extras"] is False
    assert _cli_passes(path, None) is False
