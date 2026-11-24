"""The HTTP audit coverage route does not pass a capsule the CLI gate fails.

``nova audit coverage`` exits 1 when a scanned capsule carries a ``.seal`` that fails
verification (a ``seal_invalid`` finding). ``GET /api/compliance/audit/coverage`` built its
dict by hand and dropped the finding, so a tampered capsule answered ``threshold_met: true``
with nothing to say otherwise. It now carries ``findings`` and ``gate_passed`` (the CLI's
exit decision: score at or above the threshold AND no finding); the existing keys keep their
meaning. ``POST /api/compliance/audit/report`` spreads the whole report, findings included.

A valid seal means the bytes it names are unchanged since sealing; it does not say who
signed them.
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from novafabric.capture.finalize import evidence_digests
from novafabric.serve.app import create_app
from novafabric.trust.novaseal import KeyConfig, NovaSeal

TOKEN = "audit-coverage-token"
AUTH = {"token": TOKEN}
HOST = {"host": "127.0.0.1:4321"}
KINDS = ["valid", "absent", "tampered_member", "tampered_manifest", "corrupt_envelope",
         "seal_symlink", "seal_regular_file"]
INVALID_KINDS = [k for k in KINDS if k not in ("valid", "absent")]


@pytest.fixture()
def stack(tmp_path: Path) -> tuple[TestClient, Path]:
    store = tmp_path / "runs"
    store.mkdir()
    app = create_app(token=TOKEN, capsule_dir=store, db_path=tmp_path / "registry.db")
    return TestClient(app), store


def _seal_key(work: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "coverage-test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256())
    )
    key_path, cert_path = work / "seal.key", work / "seal.crt"
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, cert_path


def _make(store: Path, kind: str, work: Path, name: str = "cap") -> Path:
    capsule = store / name
    capsule.mkdir()
    (capsule / "model-calls.jsonl").write_text('{"model":"m"}\n')
    (capsule / "capsule.yaml").write_text(yaml.safe_dump({"run_id": name, "status": "success"}))
    if kind == "absent":
        return capsule
    work.mkdir(parents=True, exist_ok=True)
    key_path, cert_path = _seal_key(work)
    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    manifest["evidence_digests"] = evidence_digests(capsule)
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    bundle = NovaSeal(
        config=KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path)),
        tsa_url="", db_path=str(work / "merkle.db"),
    ).seal(manifest)
    seal = capsule / ".seal"
    seal.mkdir()
    (seal / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal / "manifest.dsse.tsr").write_bytes(bundle.tsr)
    (seal / "log-entry.json").write_text(json.dumps(bundle.log_entry))
    if kind == "tampered_member":
        with (capsule / "model-calls.jsonl").open("a") as fh:
            fh.write('{"model":"edited"}\n')
    elif kind == "tampered_manifest":
        with (capsule / "capsule.yaml").open("a") as fh:
            fh.write("status_note: edited after sealing\n")
    elif kind == "corrupt_envelope":
        (seal / "manifest.dsse").write_bytes(b"{ not a dsse envelope")
    elif kind == "seal_symlink":
        real = work / "real-seal"
        seal.rename(real)
        os.symlink(real, seal)
    elif kind == "seal_regular_file":
        for child in seal.iterdir():
            child.unlink()
        seal.rmdir()
        seal.write_text("not a directory")
    return capsule


def _coverage(client: TestClient, threshold: float = 0.0) -> dict:  # type: ignore[type-arg]
    resp = client.get(
        "/api/compliance/audit/coverage",
        params={**AUTH, "profile": "iso42001", "threshold": threshold}, headers=HOST,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()  # type: ignore[no-any-return]


@pytest.mark.parametrize("kind", INVALID_KINDS)
def test_a_tampered_capsule_fails_the_gate_and_is_named(
    stack: tuple[TestClient, Path], tmp_path: Path, kind: str
) -> None:
    client, store = stack
    _make(store, kind, tmp_path / "w")
    body = _coverage(client)  # threshold 0.0: the score alone always passes
    assert body["threshold_met"] is True  # existing key, unchanged meaning
    assert body["gate_passed"] is False
    assert [(f["code"], f["capsule_id"]) for f in body["findings"]] == [("seal_invalid", "cap")]


@pytest.mark.parametrize("kind", ["valid", "absent"])
def test_valid_and_absent_seals_leave_the_gate_as_it_was(
    stack: tuple[TestClient, Path], tmp_path: Path, kind: str
) -> None:
    client, store = stack
    _make(store, kind, tmp_path / "w")
    body = _coverage(client)
    assert body["findings"] == []
    assert body["gate_passed"] is True and body["threshold_met"] is True


def test_the_gate_still_fails_below_the_threshold_without_a_finding(
    stack: tuple[TestClient, Path], tmp_path: Path
) -> None:
    client, store = stack
    _make(store, "absent", tmp_path / "w")
    body = _coverage(client, threshold=1.0)
    assert body["threshold_met"] is False and body["gate_passed"] is False
    assert body["findings"] == []


def test_the_report_route_carries_the_finding(
    stack: tuple[TestClient, Path], tmp_path: Path
) -> None:
    client, store = stack
    cap = _make(store, "tampered_member", tmp_path / "w")
    resp = client.post(
        "/api/compliance/audit/report", params=AUTH, headers=HOST,
        json={"run_id": cap.name, "profile": "iso42001"},
    )
    assert resp.status_code == 200, resp.text
    assert [f["code"] for f in resp.json()["findings"]] == ["seal_invalid"]
